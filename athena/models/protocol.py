"""Case protocol Firestore CRUD — protocols and protocol steps."""

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from utils import deadlines, phases
from utils.deadlines import compute_deadline as _judicial_deadline

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import concurrency, db, provenance
from security import sanitize
from utils.logging_setup import log_protocol_event, log_unexpected

logger = logging.getLogger(__name__)

# Firestore collection path
COLLECTION = "protocols"
STEPS_SUBCOLLECTION = "steps"

# Valid enum values
VALID_PROTOCOL_TYPES = ("cq_simplifié", "cs_ordinaire", "conventionnel")
VALID_STATUSES = ("actif", "complété", "suspendu")
VALID_STEP_STATUSES = ("à_venir", "en_cours", "complété", "en_retard")

# The « Prochaines » window: get_protocol_summary's `upcoming` counts open
# steps due within this many calendar days. Shared by the dossier-tab amber
# tile and the MCP summary — the value travels in the payload
# (upcoming_window_days) so consumers stop guessing what « upcoming » means.
UPCOMING_WINDOW_DAYS = 7

# Which court each C.p.c. template regime GOVERNS — the coherence gate
# (PA-D03). cq_simplifié tracks arts. 535.x (Cour du Québec simplified
# track); cs_ordinaire tracks arts. 145-148 + 173 (Superior Court);
# conventionnel is deliberately absent — it is the unrestricted escape
# hatch (admin tribunals, arbitration, appeals…). The strings match what
# models/dossier writes into `tribunal` from the juridiction table.
PROTOCOL_TYPE_TRIBUNAL = {
    "cq_simplifié": "Cour du Québec",
    "cs_ordinaire": "Cour supérieure",
}
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

# `closed_by` — WHO took a protocol out of « actif » (stamped with
# `closed_at`, both cleared when it is reactivated). CLOSED_BY_AUTO means the
# completion of its last step closed it (_check_protocol_completion); any
# other value is the provenance path of a deliberate status change (« web »,
# « mcp »…, models.provenance.VALID_VIA). `""` (absent on a legacy document)
# = not recorded. Only CLOSED_BY_AUTO is ever reopened automatically — by
# reopening one of its steps, or the task linked to one: a closure the
# lawyer (or a protocol closed before this field existed) decided stays
# closed until someone reactivates it on purpose.
CLOSED_BY_AUTO = "auto"

# The one-actif rule's refusals. Checked FAIL-CLOSED, inside the transaction
# that writes: a read error is a refusal, never « none found ».
ONE_ACTIVE_ERROR = (
    "Ce dossier a déjà un protocole actif. "
    "Complétez ou suspendez le protocole existant avant d'en créer un nouveau."
)
ACTIVE_CHECK_FAILED = (
    "Impossible de vérifier les protocoles actifs de ce dossier — réessayez. "
    "Rien n'a été enregistré."
)
OTHER_ACTIVE_ON_REOPEN = (
    "Rouvrir cette étape rouvrirait son protocole, fermé automatiquement à "
    "sa dernière étape — mais un autre protocole est actif dans ce dossier. "
    "Rien n'a été modifié."
)

# ── Protocol Templates ──────────────────────────────────────────────────
#
# Phase O: every template step carries a litigation-phase annotation
# (`phase`/`sous_phase`, utils/phases.py). This mapping is LEGAL CONTENT,
# approved with the Phase O plan (2026-08-10) — change it only on the
# practitioner's say-so. It is what makes the protocol→budget→time join
# possible, and it drives the phase default suggested on new time/expense/
# task entries (get_current_phase_for_dossier below).

CQ_TEMPLATE_STEPS = [
    {
        "order": 1,
        "title": "Signification de l'avis d'assignation",
        "description": "",
        "cpc_reference": "art. 145 C.p.c.",
        "deadline_offset_days": 0,
        "mandatory": True,
        "deadline_locked": True,
        "phase": "INT",
        "sous_phase": "INT-02",
    },
    {
        "order": 2,
        "title": "Avis de la partie demanderesse",
        "description": "",
        "cpc_reference": "art. 535.4 C.p.c.",
        "deadline_offset_days": 20,
        "mandatory": True,
        "deadline_locked": True,
        # The plaintiff's avis is her evidence disclosure → mise en état.
        "phase": "MEE",
        "sous_phase": "MEE-01",
    },
    {
        "order": 3,
        "title": "Dénonciation des moyens préliminaires",
        "description": "",
        "cpc_reference": "art. 535.5 C.p.c.",
        "deadline_offset_days": 45,
        "mandatory": True,
        "deadline_locked": True,
        "phase": "PRL",
        "sous_phase": "PRL-00",
    },
    {
        "order": 4,
        "title": "Avis de la partie défenderesse",
        "description": "",
        "cpc_reference": "art. 535.6 C.p.c.",
        "deadline_offset_days": 95,
        "mandatory": True,
        "deadline_locked": True,
        # The defendant's avis is its contestation-side disclosure → défense.
        "phase": "CTS",
        "sous_phase": "CTS-01",
    },
    {
        "order": 5,
        "title": "Conférence de gestion",
        "description": "",
        "cpc_reference": "art. 535.8 C.p.c.",
        "deadline_offset_days": 110,
        "mandatory": True,
        "deadline_locked": True,
        "phase": "MEE",
        "sous_phase": "MEE-03",
    },
    {
        "order": 6,
        "title": "Conférence de règlement à l'amiable",
        "description": "",
        "cpc_reference": "art. 535.12 C.p.c.",
        "deadline_offset_days": 145,
        "mandatory": True,
        "deadline_locked": True,
        "phase": "PRD",
        "sous_phase": "PRD-03",
    },
    {
        "order": 7,
        "title": "Inscription pour instruction et jugement",
        "description": "",
        "cpc_reference": "art. 535.13 C.p.c.",
        "deadline_offset_days": 180,
        "mandatory": True,
        "deadline_locked": True,
        "phase": "INS",
        "sous_phase": "INS-01",
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
        "phase": "INT",
        "sous_phase": "INT-02",
    },
    {
        "order": 2,
        "title": "Réponse",
        "description": "",
        "cpc_reference": "art. 145(2) C.p.c.",
        "deadline_offset_days": 15,
        "mandatory": True,
        "deadline_locked": False,
        # The réponse is the defendant's first act — contestation begins,
        # but it is not yet a written defence → the phase's -00.
        "phase": "CTS",
        "sous_phase": "CTS-00",
    },
    {
        "order": 3,
        "title": "Premier protocole de l'instance",
        "description": "",
        "cpc_reference": "art. 149(2) C.p.c.",
        "deadline_offset_days": 45,
        "mandatory": True,
        "deadline_locked": False,
        "phase": "INT",
        "sous_phase": "INT-03",
    },
    {
        "order": 4,
        "title": "Interrogatoires préalables",
        "description": "",
        "cpc_reference": "",
        "deadline_offset_days": 120,
        "mandatory": True,
        "deadline_locked": False,
        "phase": "INR",
        "sous_phase": "INR-00",
    },
    {
        "order": 5,
        "title": "Expertises (rapports d'experts)",
        "description": "",
        "cpc_reference": "",
        "deadline_offset_days": 150,
        "mandatory": True,
        "deadline_locked": False,
        "phase": "EXP",
        "sous_phase": "EXP-02",
    },
    {
        "order": 6,
        "title": "Conférence de règlement à l'amiable",
        "description": "",
        "cpc_reference": "",
        "deadline_offset_days": 180,
        "mandatory": True,
        "deadline_locked": False,
        "phase": "PRD",
        "sous_phase": "PRD-03",
    },
    {
        "order": 7,
        "title": "Conférence de gestion",
        "description": "",
        "cpc_reference": "",
        "deadline_offset_days": 180,
        "mandatory": True,
        "deadline_locked": False,
        "phase": "MEE",
        "sous_phase": "MEE-03",
    },
    {
        "order": 8,
        "title": "Inscription pour instruction et jugement",
        "description": "",
        "cpc_reference": "art. 173(1) C.p.c.",
        "deadline_offset_days": 180,
        "mandatory": True,
        "deadline_locked": False,
        "phase": "INS",
        "sous_phase": "INS-01",
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
        "closed_by": "",
        "closed_at": None,
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
        # Phase O — litigation-phase annotation ("" = unannotated, e.g. a
        # custom step the user did not classify, or a conventionnel protocol)
        "phase": "",
        "sous_phase": "",
        "created_at": None,
        "updated_at": None,
        # Rule 7, since lot 1a: every write to the step regenerates it —
        # except check_overdue_steps' derived « en_retard » stamp, which a
        # mere page view applies and which must not invalidate the etag a
        # caller holds. '' on a step written before steps carried one.
        "etag": "",
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


def regime_mismatch(protocol_type: str, dossier: Optional[dict]) -> bool:
    """True when the template's C.p.c. regime cannot govern this dossier.

    The live case that motivated the gate: a cq_simplifié protocol (arts.
    535.x C.p.c.) active on a Superior Court file — deadlines tracked from a
    Code regime that does not govern it. Pure predicate, shared by the
    creation gate, the wizard annotation and the MCP regime_mismatch flag,
    so the three surfaces cannot drift.

    Conservative on unknowns: no dossier, a conventionnel template, or a
    dossier whose tribunal is blank (préjudiciaire, unparsed) → False —
    there is nothing to validate against, and refusing would strand the
    file. A non-judicial forum (administratif/federal) mismatches BOTH
    C.p.c. templates.
    """
    if not dossier:
        return False
    expected = PROTOCOL_TYPE_TRIBUNAL.get(protocol_type)
    if not expected:
        return False
    if (dossier.get("forum_type") or "judiciaire") in ("administratif", "federal"):
        return True
    tribunal = (dossier.get("tribunal") or "").strip()
    return bool(tribunal) and tribunal != expected


def _regime_errors(dossier_id: str, protocol_type: str) -> list[str]:
    """French, actionable refusal when the template regime is incoherent."""
    expected = PROTOCOL_TYPE_TRIBUNAL.get(protocol_type)
    if not expected:
        return []
    # Deferred function-level import — the house pattern for the
    # protocol↔dossier edge (see _sync_task_status / _auto_create_tasks).
    from models.dossier import get_dossier
    dossier = get_dossier(dossier_id)
    if not regime_mismatch(protocol_type, dossier):
        return []
    label = PROTOCOL_TYPE_LABELS.get(protocol_type, protocol_type)
    tribunal = (dossier.get("tribunal") or "").strip()
    if (dossier.get("forum_type") or "judiciaire") in ("administratif", "federal"):
        where = tribunal or "un forum non judiciaire"
        return [
            f"Le gabarit « {label} » suit le C.p.c., mais ce dossier est "
            f"devant {where}. Utilisez le gabarit « Conventionnel »."
        ]
    suggestion = next(
        (
            PROTOCOL_TYPE_LABELS[t]
            for t, trib in PROTOCOL_TYPE_TRIBUNAL.items()
            if trib == tribunal
        ),
        PROTOCOL_TYPE_LABELS["conventionnel"],
    )
    return [
        f"Le gabarit « {label} » vise la {expected}, mais ce dossier est "
        f"devant la {tribunal}. Utilisez le gabarit « {suggestion} »."
    ]


def _validate_step(data: dict) -> list[str]:
    """Return a list of validation error messages for a step."""
    errors: list[str] = []

    if not data.get("title", "").strip():
        errors.append("Le titre de l'étape est requis.")

    status = data.get("status", "")
    if status and status not in VALID_STEP_STATUSES:
        errors.append("Statut d'étape invalide.")

    errors.extend(phases.validate_pair(data))

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


class _Refusal(Exception):
    """A refusal decided inside a model transaction — nothing was written.

    Raised from inside the ``@firestore.transactional`` body, so the real
    decorator rolls the transaction back and re-raises it; the caller turns
    it into its ``(None, messages)`` return and a typed log line carrying
    the machine-stable ``reason`` (never the French text).
    """

    def __init__(self, messages, reason: str) -> None:
        super().__init__(reason)
        self.messages = [messages] if isinstance(messages, str) else list(messages)
        self.reason = reason


def _get_active_protocols_strict(
    dossier_id: str,
    *,
    exclude_id: Optional[str] = None,
    transaction=None,
) -> list[dict]:
    """The dossier's « actif » protocols — a read failure PROPAGATES.

    The one-actif rule used to read through a helper that answered ``[]``
    on ANY exception: a Firestore blip read as « no active protocol » and a
    second one was minted. Every caller now turns an exception into a
    refusal. Pass the caller's ``transaction`` so the query joins its read
    set: a protocol activated between this read and the commit then aborts
    the transaction (and its retry sees it) instead of slipping past.

    Two equality filters, merged by Firestore from the automatic
    single-field indexes — no composite index.
    """
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("dossier_id", "==", dossier_id)
    ).where(
        filter=FieldFilter("status", "==", "actif")
    )
    return [
        snap.to_dict() or {}
        for snap in query.stream(transaction=transaction)
        if snap.id != exclude_id
    ]


def _step_linked_to(protocol_id: str, task_id: str) -> Optional[dict]:
    """The step of *protocol_id* whose ``linked_task_id`` is *task_id*.

    A collection-scoped equality on the step subcollection (automatic
    index). Propagates read errors.
    """
    steps = (
        db.collection(COLLECTION).document(protocol_id)
        .collection(STEPS_SUBCOLLECTION)
        .where(filter=FieldFilter("linked_task_id", "==", task_id))
        .limit(1)
        .stream()
    )
    for snap in steps:
        step = dict(snap.to_dict() or {})
        step.setdefault("id", snap.id)
        return step
    return None


def _created_sort_key(proto: dict) -> float:
    created = proto.get("created_at")
    return -created.timestamp() if isinstance(created, datetime) else 0.0


def find_step_for_task(
    task_id: str, dossier_id: Optional[str]
) -> Optional[tuple[dict, dict]]:
    """``(protocol, step)`` for the step linked to *task_id*, or ``None``.

    Searched in the task's own dossier first — EVERY protocol there, actif
    first then newest, since a step of a protocol closed by the cascade
    must still be found when its task is reopened — then, as a fallback,
    every « actif » protocol of the firm: a task moved to another dossier
    before moves of linked tasks were refused keeps its step in the old
    dossier's protocol, and the cascade has always scanned the firm-wide
    actif protocols to reach it. What this returns is therefore what the
    cascade acts on — never a narrower guess.

    The protocol returned is its document, without steps. PROPAGATES read
    errors: callers decide between refusing (a move) and logging (a
    cascade), and « not found » must never stand in for « unreadable ».
    No collection-group query — so no fieldOverride, no composite index.
    """
    if not task_id:
        return None
    seen: set[str] = set()
    if dossier_id:
        snaps = list(
            db.collection(COLLECTION)
            .where(filter=FieldFilter("dossier_id", "==", dossier_id))
            .stream()
        )
        protos = sorted(
            ((snap.id, snap.to_dict() or {}) for snap in snaps),
            key=lambda item: (item[1].get("status") != "actif",
                              _created_sort_key(item[1])),
        )
        for protocol_id, proto in protos:
            seen.add(protocol_id)
            step = _step_linked_to(protocol_id, task_id)
            if step is not None:
                return {**proto, "id": proto.get("id") or protocol_id}, step
    active = (
        db.collection(COLLECTION)
        .where(filter=FieldFilter("status", "==", "actif"))
        .stream()
    )
    for snap in active:
        if snap.id in seen:
            continue
        step = _step_linked_to(snap.id, task_id)
        if step is not None:
            proto = snap.to_dict() or {}
            return {**proto, "id": proto.get("id") or snap.id}, step
    return None


# ── CRUD ────────────────────────────────────────────────────────────────


# What a caller may name on a new protocol — everything else is set here
# (id, type, dossier, dates, steps) or server-owned (status starts « actif »,
# closed_by/closed_at, the stamps). Refused, never dropped: a key outside
# this list is a caller that believes it set something.
_CREATE_FIELDS = frozenset({
    "title", "notes", "court", "dossier_file_number", "dossier_title",
})


def _unknown_field_error(key: str, what: str) -> str:
    return f"Le champ « {key} » {what} ne se modifie pas ainsi."


def create_protocol(
    dossier_id: str,
    protocol_type: str,
    start_date: datetime,
    data: dict,
    auto_create_tasks: bool = False,
) -> tuple[Optional[dict], list[str]]:
    """Create a protocol with auto-generated steps. Returns (doc, errors).

    The one-actif rule is checked INSIDE the transaction that writes the
    protocol and its steps, on a query that joins the transaction's read
    set, and FAILS CLOSED: a read error refuses (``ACTIVE_CHECK_FAILED``)
    instead of reading as « no active protocol », and a protocol activated
    between the check and the commit aborts it — the retry then sees it.
    A new protocol is always « actif ».

    Each step is stamped (``created_*``/``updated_*``/``etag``) like any
    document. ``auto_create_tasks`` creates one linked task per step after
    the commit (:func:`create_linked_tasks`); the returned steps carry the
    links it made.
    """
    unknown = sorted(set(data) - _CREATE_FIELDS)
    if unknown:
        log_protocol_event("protocol_refused", "", outcome="refused",
                           reason="champ_refuse", operation="create",
                           dossier_id=dossier_id)
        return None, [_unknown_field_error(unknown[0], "d'un protocole")]

    merged = {**_default_protocol(), **_sanitize_data(data)}
    merged["dossier_id"] = dossier_id
    merged["protocol_type"] = protocol_type
    merged["start_date"] = start_date

    errors = _validate_protocol(merged)
    if errors:
        log_protocol_event("protocol_refused", "", outcome="refused",
                           reason="validation", operation="create",
                           dossier_id=dossier_id)
        return None, errors

    # Regime/forum coherence gate (PA-D03): a C.p.c. template whose court
    # disagrees with the dossier's tribunal is refused with the expected
    # template named — a protocol tracking the wrong Code is a litigation
    # risk, not a preference.
    regime = _regime_errors(dossier_id, protocol_type)
    if regime:
        log_protocol_event("protocol_refused", "", outcome="refused",
                           reason="regime", operation="create",
                           dossier_id=dossier_id)
        return None, regime

    now = datetime.now(timezone.utc)
    protocol_id = str(uuid.uuid4())

    # Generate steps from template
    template_steps = get_template(protocol_type)
    step_docs = []
    for tmpl in template_steps:
        step = {**_default_step(), **tmpl}
        step["id"] = str(uuid.uuid4())
        step.update(provenance.create_fields(now))
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
        "status": "actif",
        **provenance.create_fields(now),
    })

    proto_ref = db.collection(COLLECTION).document(protocol_id)

    @firestore.transactional
    def _create(txn) -> None:
        # The read comes first (the real client refuses a transactional read
        # after a staged write) and is the one-actif check itself.
        try:
            active = _get_active_protocols_strict(dossier_id, transaction=txn)
        except Exception:
            log_unexpected("protocol one-actif check failed",
                           dossier_id=dossier_id)
            raise _Refusal(ACTIVE_CHECK_FAILED, "lecture_impossible")
        if active:
            raise _Refusal(ONE_ACTIVE_ERROR, "protocole_actif_existant")
        txn.set(proto_ref, merged)
        for step in step_docs:
            txn.set(
                proto_ref.collection(STEPS_SUBCOLLECTION).document(step["id"]),
                step,
            )

    try:
        _create(db.transaction())
    except _Refusal as refusal:
        log_protocol_event("protocol_refused", "", outcome="refused",
                           reason=refusal.reason, operation="create",
                           dossier_id=dossier_id)
        return None, refusal.messages
    except Exception:
        log_unexpected("protocol write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, protocol_id)
    log_protocol_event("protocol_created", protocol_id,
                       dossier_id=dossier_id, protocol_type=protocol_type,
                       step_count=len(step_docs))

    # Auto-create linked tasks if requested
    if auto_create_tasks and step_docs:
        create_linked_tasks(protocol_id, merged, step_docs)

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


def get_current_phase_for_dossier(dossier_id: str) -> tuple[str, str]:
    """The dossier's current litigation phase, derived from its protocol.

    « L'étape courante » = the first step, in ``order``, whose status is not
    « complété » (the implicit logic of ``_check_protocol_completion``). Its
    ``(phase, sous_phase)`` annotation is the suggested default for a new
    time/expense/task entry (D-6 §10). Returns ``("", "")`` when there is no
    active protocol, no annotated open step (conventionnel), or no dossier.

    COST: ~10 Firestore reads (protocol query + steps subcollection). Callers
    pay it ONLY on a form GET that already knows its dossier — never on the
    DAV path, never on a blank form (the ``_linked_step`` short-circuit rule).
    """
    if not dossier_id:
        return "", ""
    try:
        protocol = get_protocol_for_dossier(dossier_id, active_only=True)
        if not protocol:
            return "", ""
        for step in protocol.get("steps", []):  # already sorted by order
            if step.get("status") != "complété":
                return step.get("phase", "") or "", step.get("sous_phase", "") or ""
        return "", ""
    except Exception:
        # A suggestion must never break a form render.
        return "", ""


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
    provenance.stamp_update(merged, now)

    try:
        db.collection(COLLECTION).document(protocol_id).set(merged)
    except Exception:
        log_unexpected("protocol write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

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
    except Exception:
        log_unexpected("protocol delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


# ── Step operations ─────────────────────────────────────────────────────


def add_step(
    protocol_id: str, step_data: dict
) -> tuple[Optional[dict], list[str]]:
    """Add a custom step to a protocol. Returns (step_doc, errors)."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return None, ["Protocole introuvable."]

    merged = {**_default_step(), **_sanitize_data(step_data)}
    phases.apply_sous_phase_default(merged)

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
        "date_confirmed": True,
        **provenance.create_fields(now),
    })

    try:
        db.collection(COLLECTION).document(protocol_id).collection(
            STEPS_SUBCOLLECTION
        ).document(step_id).set(merged)

        # Update protocol etag and updated_at
        db.collection(COLLECTION).document(protocol_id).update(
            provenance.update_fields(now)
        )
    except Exception:
        log_unexpected("protocol write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

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
    provenance.stamp_update(merged, now)

    # If user explicitly sets a date on CS protocol, mark as confirmed
    if "deadline_date" in data:
        merged["date_confirmed"] = True

    try:
        db.collection(COLLECTION).document(protocol_id).collection(
            STEPS_SUBCOLLECTION
        ).document(step_id).set(merged)

        db.collection(COLLECTION).document(protocol_id).update(
            provenance.update_fields(now)
        )
    except Exception:
        log_unexpected("protocol write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

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

        db.collection(COLLECTION).document(protocol_id).update(
            provenance.update_fields(now)
        )
        return True, ""
    except Exception:
        log_unexpected("protocol delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


# The two states a one-click step control can ask for. `en_cours` and
# `en_retard` are never a target: `en_retard` is derived from the deadline
# (check_overdue_steps), and neither is offered by any control.
STEP_STATUS_TARGETS: tuple[str, ...] = ("complété", "à_venir")


def _step_already_at(status: str, target: str) -> bool:
    """True when a step in *status* already is what *target* asks for.

    « à_venir » asks for an OPEN step, so a step already open in any form —
    à_venir, en_cours, or en_retard (the deadline-derived stamp) — is
    already there: reopening it would erase the en_cours/en_retard state
    for nothing.
    """
    if target == "complété":
        return status == "complété"
    return status != "complété"


def _inactive_protocol_error(status: str) -> str:
    label = STATUS_LABELS.get(status, status).lower()
    return (
        f"Ce protocole est « {label} » : réactivez-le (Modifier le protocole "
        "→ Statut « Actif ») avant de changer l'état de ses étapes. Rien n'a "
        "été modifié."
    )


def _reopen_blocker(protocol: dict, protocol_id: str, txn) -> Optional[_Refusal]:
    """Why an auto-closed protocol cannot be reopened now, or ``None``.

    Only a protocol the CASCADE closed (``closed_by == CLOSED_BY_AUTO``)
    reopens by itself; the one-actif rule then applies as everywhere, read
    fail-closed inside the caller's transaction.
    """
    try:
        others = _get_active_protocols_strict(
            protocol.get("dossier_id") or "", exclude_id=protocol_id,
            transaction=txn,
        )
    except Exception:
        log_unexpected("protocol one-actif check failed",
                       protocol_id=protocol_id)
        return _Refusal(ACTIVE_CHECK_FAILED, "lecture_impossible")
    if others:
        return _Refusal(OTHER_ACTIVE_ON_REOPEN, "autre_protocole_actif")
    return None


def set_step_status(
    protocol_id: str,
    step_id: str,
    target: str,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Complete (``complété``) or reopen (``à_venir``) a step — never a toggle.

    Returns ``(step, errors, outcome)``. ``outcome`` reports what happened
    beyond the step — ``changed`` (False: the step already was *target*, and
    NOTHING was written or cascaded), ``task_id`` (the linked task, if any),
    ``task_sync`` (:func:`_sync_task_status`'s word, or ``"none"``),
    ``protocol_closed`` (completing the last open step closes the protocol)
    and ``protocol_reopened`` (reopening a step of a protocol the cascade
    had closed reactivates it).

    Why not :func:`complete_step`: it toggles, so a page rendered before the
    phone, a second tab or the connector changed the step does the OPPOSITE
    of what the lawyer clicked, and cascades that opposite into the linked
    task. Here the caller names the state it wants, and a step already in it
    is a no-op — no etag churn, no cascade, no CTag bump — whatever
    ``expected_etag`` says: a request that changes nothing needs neither a
    current version nor an open protocol.

    Otherwise, in this order, all of it decided and staged in ONE
    transaction on the step and protocol it re-reads (a write landing in
    between aborts it; the retry decides again on the new state):

    1. ``expected_etag`` (optional) must still be the step's etag;
    2. the PROTOCOL gate — an « actif » protocol accepts both targets; a
       protocol the cascade closed (``closed_by == CLOSED_BY_AUTO``)
       accepts a reopen, which reactivates it; anything else (suspended, or
       completed by a deliberate change or before ``closed_by`` existed) is
       refused, naming how to reactivate it;
    3. a reactivation must be feasible — no OTHER « actif » protocol in the
       dossier, read fail-closed — or the request is refused BEFORE any
       write: the step never lands open inside a closed protocol;
    4. the step's partial ``update()`` (status, completed_date, its stamp
       and etag) and the protocol's (its stamp, plus status « actif » and
       cleared ``closed_by``/``closed_at`` on a reactivation) commit
       together.

    The task cascade and the completion check run after the commit — they
    follow a committed write, so they report instead of raising
    (``outcome``), as they always have.
    """
    outcome: dict = {
        "changed": False, "task_id": None, "task_sync": "none",
        "protocol_closed": False, "protocol_reopened": False,
    }
    if target not in STEP_STATUS_TARGETS:
        log_protocol_event("step_status_refused", protocol_id,
                           outcome="refused", reason="statut_invalide",
                           step_id=step_id)
        return None, ["Statut d'étape demandé invalide."], outcome

    proto_ref = db.collection(COLLECTION).document(protocol_id)
    step_ref = proto_ref.collection(STEPS_SUBCOLLECTION).document(step_id)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def _apply(txn) -> tuple[dict, str, bool, bool]:
        # Reads first: the real client refuses a transactional read after
        # a staged write.
        proto_snap = proto_ref.get(transaction=txn)
        step_snap = step_ref.get(transaction=txn)
        if not proto_snap.exists:
            raise _Refusal("Protocole introuvable.", "protocole_introuvable")
        if not step_snap.exists:
            raise _Refusal("Étape introuvable.", "etape_introuvable")
        protocol = proto_snap.to_dict() or {}
        step = dict(step_snap.to_dict() or {})
        step.setdefault("id", step_id)
        before = step.get("status", "")
        if _step_already_at(before, target):
            return step, before, False, False
        if not concurrency.matches(step, expected_etag):
            raise _Refusal(concurrency.STALE_ETAG_ERROR, "stale_etag")
        reopen = False
        status = protocol.get("status", "")
        if status != "actif":
            if not (status == "complété" and target == "à_venir"
                    and protocol.get("closed_by") == CLOSED_BY_AUTO):
                raise _Refusal(_inactive_protocol_error(status),
                               "protocole_non_actif")
            blocker = _reopen_blocker(protocol, protocol_id, txn)
            if blocker is not None:
                raise blocker
            reopen = True
        fields = {
            "status": target,
            "completed_date": now if target == "complété" else None,
            **provenance.update_fields(now),
        }
        txn.update(step_ref, fields)
        proto_fields = provenance.update_fields(now)
        if reopen:
            proto_fields.update(status="actif", closed_by="", closed_at=None)
        txn.update(proto_ref, proto_fields)
        return {**step, **fields}, before, True, reopen

    try:
        step, before, changed, reopened = _apply(db.transaction())
    except _Refusal as refusal:
        log_protocol_event("step_status_refused", protocol_id,
                           outcome="refused", reason=refusal.reason,
                           step_id=step_id)
        return None, refusal.messages, outcome
    except Exception:
        log_unexpected("protocol step status write failed",
                       protocol_id=protocol_id, step_id=step_id)
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."], outcome

    if not changed:
        return step, [], outcome
    provenance.note_commit(COLLECTION, protocol_id)

    outcome["changed"] = True
    outcome["protocol_reopened"] = reopened
    linked = step.get("linked_task_id")
    if linked:
        outcome["task_id"] = linked
        outcome["task_sync"] = _sync_task_status(
            linked, target, protocol_id=protocol_id)
    if target == "complété":
        outcome["protocol_closed"] = _check_protocol_completion(protocol_id)

    log_protocol_event(
        "step_status_set", protocol_id, step_id=step_id,
        from_status=before, to_status=target,
        task_sync=outcome["task_sync"],
        protocol_closed=outcome["protocol_closed"],
        protocol_reopened=reopened,
    )
    return step, [], outcome


def complete_step(
    protocol_id: str, step_id: str
) -> tuple[Optional[dict], list[str]]:
    """DEPRECATED toggle, kept for compatibility. Returns (step_doc, errors).

    It reads the step and asks :func:`set_step_status` for the OPPOSITE
    state — so a caller holding a stale view still flips it the wrong way.
    That is why nothing in ``routes/`` or ``mcp/`` calls it any more (a test
    pins the absence): name the target with :func:`set_step_status`.
    """
    protocol = get_protocol(protocol_id)
    if not protocol:
        return None, ["Protocole introuvable."]

    target_step = next(
        (s for s in protocol.get("steps", []) if s.get("id") == step_id), None
    )
    if not target_step:
        return None, ["Étape introuvable."]

    target = "à_venir" if target_step.get("status") == "complété" else "complété"
    step, errors, _outcome = set_step_status(protocol_id, step_id, target)
    return step, errors


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
                provenance.stamp_update(step, now)

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
            **provenance.update_fields(now),
        })

        batch.commit()
    except Exception:
        log_unexpected("protocol deadline recompute failed")
        return None, ["Erreur lors du recalcul. Veuillez réessayer."]

    return get_protocol(protocol_id), []


def check_overdue_steps(protocol_id: str) -> int:
    """Scan steps and update status to en_retard where overdue. Returns count.

    Calendar-date rule (shared with get_protocol_summary and the MCP step
    row): a step due TODAY is not overdue yet — the old wall-clock compare
    flipped the stored status to en_retard at 00:00 UTC on the due date,
    a day earlier than every read surface claimed.
    """
    protocol = get_protocol(protocol_id)
    if not protocol:
        return 0

    now = datetime.now(timezone.utc)
    # The Montréal calendar day, against the PROROGUED deadline — the same
    # rule every read surface applies since 2026-08-02. The old UTC-date
    # compare stamped « en_retard » from 20:00 EDT the evening before, and
    # nothing ever clears the stamp.
    today = deadlines.today_mtl()
    count = 0

    for step in protocol.get("steps", []):
        deadline = step.get("deadline_date")
        if (
            deadline
            and deadlines.is_past_due(deadline, today=today)
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
                except Exception as exc:
                    logger.warning("check_overdue_steps: failed to mark step %s overdue: %s", step.get("id"), exc)
            else:
                count += 1

    return count


def list_urgent_steps(cutoff: datetime, limit: int = 50) -> list[dict]:
    """Return non-completed steps of active protocols due on/before *cutoff*.

    Replaces the dashboard N+1 (one ``steps`` stream per active protocol)
    with a single COLLECTION GROUP query on the ``steps`` subcollection plus
    one batched ``get_all`` round-trip for the distinct parent protocols.

    The non-completed filter is applied server-side with an ``in`` filter
    over the active step statuses (NOT ``!=``, which complicates indexes) so
    that old completed steps cannot crowd current ones out of the bounded
    result window as data grows; a Python-side check is kept as
    belt-and-suspenders. Requires the COLLECTION_GROUP composite index on
    ``steps`` (status ASC, deadline_date ASC); see ``firestore.indexes.json``.

    Each returned step dict is enriched with ``_protocol_title``,
    ``_protocol_id`` and ``_dossier_file_number`` from its parent protocol;
    steps whose parent protocol is missing or not ``actif`` are dropped.
    Results keep the query's deadline_date ascending order.

    Returns [] on failure (the dashboard degrades gracefully).
    """
    active_step_statuses = [s for s in VALID_STEP_STATUSES if s != "complété"]
    try:
        # Over-fetch: steps of suspended/completed protocols pass the
        # server-side filters (protocol status lives on the parent) and are
        # dropped after the join below — a 3× window keeps them from crowding
        # genuinely active steps out of the bounded result.
        query = (
            db.collection_group(STEPS_SUBCOLLECTION)
            .where(filter=FieldFilter("status", "in", active_step_statuses))
            .where(filter=FieldFilter("deadline_date", "<=", cutoff))
            .order_by("deadline_date")
            .limit(limit * 3)
        )
        snapshots = list(query.stream())
    except Exception as exc:
        logger.warning("list_urgent_steps: collection-group query failed: %s", exc)
        return []

    # Pair each surviving step with its parent protocol id, deduping parents.
    candidates: list[tuple[dict, str]] = []
    parent_refs: dict[str, object] = {}
    for snap in snapshots:
        step = snap.to_dict() or {}
        if step.get("status") == "complété":
            continue  # defensive — already excluded server-side
        # steps live at protocols/{protocolId}/steps/{stepId}
        parent_ref = snap.reference.parent.parent
        if parent_ref is None:
            continue
        parent_refs[parent_ref.id] = parent_ref
        candidates.append((step, parent_ref.id))

    if not candidates:
        return []

    # Single round-trip fetch of all distinct parent protocols.
    protocols: dict[str, dict] = {}
    try:
        for proto_snap in db.get_all(list(parent_refs.values())):
            if proto_snap.exists:
                protocols[proto_snap.id] = proto_snap.to_dict() or {}
    except Exception as exc:
        logger.warning("list_urgent_steps: parent protocol fetch failed: %s", exc)
        return []

    urgent_steps: list[dict] = []
    for step, protocol_id in candidates:
        proto = protocols.get(protocol_id)
        if not proto or proto.get("status") != "actif":
            continue
        step["_protocol_title"] = proto.get("title", "")
        step["_protocol_id"] = proto.get("id", protocol_id)
        step["_dossier_file_number"] = proto.get("dossier_file_number", "")
        # The live dossier id, so consumers can refresh the (possibly
        # stale) denormalized label above with a batched join (PA-D04).
        step["_dossier_id"] = proto.get("dossier_id", "")
        urgent_steps.append(step)
        if len(urgent_steps) >= limit:
            break

    if len(urgent_steps) >= limit:
        logger.info("list_urgent_steps: result window full (limit=%d)", limit)
    return urgent_steps


# ── Summary ─────────────────────────────────────────────────────────────


_SUMMARY_UNSET = object()


def get_protocol_summary(
    dossier_id: str,
    today: Optional[date] = None,
    *,
    protocol: object = _SUMMARY_UNSET,
    protocols: Optional[list[dict]] = None,
) -> dict:
    """Return protocol summary for a dossier (active protocol only).

    One rule on every surface since 2026-08-02: the Montréal calendar day,
    against the PROROGUED deadline. The web caller (the dossier's Protocole
    tab tiles) aligns through the default; the MCP passes the same value
    explicitly so one response shares one clock read.

    ``protocol``/``protocols`` let a caller that already holds the active
    protocol (with steps) and the dossier's protocol list reuse them instead
    of re-reading — the Protocole tab was paying the same document + steps
    stream four times per render. ``protocol`` distinguishes « not supplied »
    (sentinel → read here) from « supplied as None » (caller affirmed no
    active protocol exists).
    """
    if protocol is _SUMMARY_UNSET:
        protocol = get_protocol_for_dossier(dossier_id, active_only=True)
    if not protocol:
        all_protos = (
            protocols
            if protocols is not None
            else list_protocols_for_dossier(dossier_id)
        )
        return {
            "has_protocol": False,
            "has_history": len(all_protos) > 0,
            "total": 0,
            "completed": 0,
            "overdue": 0,
            "upcoming": 0,
            "upcoming_window_days": UPCOMING_WINDOW_DAYS,
            "next_deadline_date": None,
        }

    steps = protocol.get("steps", [])
    # Calendar-date rule, shared with the MCP step row: deadline_date is
    # date-only (midnight UTC), so comparisons run on UTC calendar dates —
    # a step due TODAY is upcoming, never overdue (the old wall-clock
    # comparison flipped it to overdue at 00:00 UTC while the MCP row said
    # is_overdue: false, a cross-tool contradiction on the same document).
    today = today or deadlines.today_mtl()
    window_end = today + timedelta(days=UPCOMING_WINDOW_DAYS)
    completed = [s for s in steps if s.get("status") == "complété"]
    open_dated = [
        (s, s["deadline_date"].astimezone(timezone.utc).date())
        for s in steps
        if s.get("deadline_date") and s.get("status") not in ("complété",)
    ]
    overdue = [
        s for s, d in open_dated
        if deadlines.is_past_due(d, today=today)
    ]
    upcoming = [s for s, d in open_dated if today <= d <= window_end]
    next_deadline = min((d for _, d in open_dated), default=None)

    all_protos = (
        protocols
        if protocols is not None
        else list_protocols_for_dossier(dossier_id)
    )
    return {
        "has_protocol": True,
        "has_history": len(all_protos) > 1,
        "protocol_id": protocol["id"],
        "protocol_type": protocol.get("protocol_type", ""),
        "total": len(steps),
        "completed": len(completed),
        "overdue": len(overdue),
        # « upcoming » was an unnamed 7-day web-tile window leaking into the
        # MCP contract as if it meant « all future steps » (PA-D05) — the
        # window now travels with the count, and next_deadline_date is the
        # field a caller actually wants.
        "upcoming": len(upcoming),
        "upcoming_window_days": UPCOMING_WINDOW_DAYS,
        "next_deadline_date": (
            next_deadline.isoformat() if next_deadline else None
        ),
    }


# ── Task sync helpers ───────────────────────────────────────────────────


def _link_task_to_step(protocol_id: str, step_id: str, task_id: str) -> bool:
    """Point a step at its linked task. True when the link committed.

    A write to the step like any other: its stamp and etag move, and so
    does the protocol's, in ONE batch. Never raises — the task it links has
    already been created; a failed link is an orphan task, logged by ids.
    """
    now = datetime.now(timezone.utc)
    proto_ref = db.collection(COLLECTION).document(protocol_id)
    try:
        batch = db.batch()
        batch.update(
            proto_ref.collection(STEPS_SUBCOLLECTION).document(step_id),
            {"linked_task_id": task_id, **provenance.update_fields(now)},
        )
        batch.update(proto_ref, provenance.update_fields(now))
        batch.commit()
    except Exception:
        log_unexpected("protocol linked task: step link failed",
                       protocol_id=protocol_id, step_id=step_id,
                       task_id=task_id)
        return False
    provenance.note_commit(COLLECTION, protocol_id)
    return True


def create_linked_tasks(
    protocol_id: str, protocol: dict, steps: list[dict]
) -> dict:
    """Create one linked task per step; return what happened.

    ``{"created", "linked", "failed"}`` counts. Each step is its own
    attempt: one outer ``try`` used to abort every remaining step on the
    first exception, silently, leaving the rest of the protocol without its
    tasks. A step that got its task has ``linked_task_id`` set in *steps*
    (the caller's list), so a caller returning them shows the links.

    The per-dossier CTag is bumped HERE, once per task created — a
    documented in-model bump (the tasks are DAV-exposed and created behind
    the caller's back), each guarded on its own: the task it announces has
    committed.
    """
    from dav.sync import bump_ctag, collection_for
    from models.task import create_task

    dossier_id = protocol.get("dossier_id")
    report = {"created": 0, "linked": 0, "failed": 0}
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
            # Phase O: the linked task inherits its step's annotation, so
            # time logged against the task's phase matches the protocol.
            "phase": step.get("phase", ""),
            "sous_phase": step.get("sous_phase", ""),
        }
        try:
            task, _errors = create_task(task_data)
        except Exception:
            log_unexpected("protocol linked task creation failed",
                           protocol_id=protocol_id, step_id=step.get("id"))
            task = None
        if not task:
            report["failed"] += 1
            continue
        report["created"] += 1
        try:
            bump_ctag(collection_for(dossier_id))
        except Exception:
            log_unexpected("protocol linked task CTag bump failed",
                           protocol_id=protocol_id, task_id=task["id"])
        if _link_task_to_step(protocol_id, step["id"], task["id"]):
            report["linked"] += 1
            step["linked_task_id"] = task["id"]
    log_protocol_event(
        "linked_tasks_created", protocol_id,
        outcome="success" if not report["failed"] and
        report["linked"] == report["created"] else "refused",
        **report,
    )
    return report


_SYNCING: set[str] = set()  # Circular sync guard


def cascaded_task_status(
    step_status: str, task_status: str
) -> tuple[Optional[str], Optional[str]]:
    """What a step's new status does to its linked task — pure.

    Returns ``(new_task_status, skip_reason)``: ``(None, None)`` means the
    task already agrees (nothing to write); a ``skip_reason`` means the
    task is deliberately LEFT ALONE and the skip is worth a trace.

    * A CANCELLED task is never touched, in either direction. Completing
      its step used to turn « annulée » into « terminée », reopening it
      turned it into « à_faire » — the lawyer's cancellation silently
      rewritten, the harm ``complete_task`` already refuses on the task
      side (``tache_annulee``).
    * ``complété`` → the task is done (``terminée``).
    * ``à_venir`` (a reopen) → a DONE task reopens to ``à_faire``; an open
      one (``à_faire`` or ``en_cours``) is already « not done » and keeps
      its state — the old cascade demoted ``en_cours`` to ``à_faire``.
    * ``en_cours`` → ``à_faire`` advances to ``en_cours``; a done task is
      never downgraded by it (``tache_terminee``).
    * Any other step status (``en_retard`` is a deadline stamp) cascades
      nothing.
    """
    if step_status not in ("complété", "à_venir", "en_cours"):
        return None, None
    if task_status == "annulée":
        return None, "tache_annulee"
    if step_status == "complété":
        return (None, None) if task_status == "terminée" else ("terminée", None)
    if step_status == "à_venir":
        return ("à_faire", None) if task_status == "terminée" else (None, None)
    # step en_cours
    if task_status == "terminée":
        return None, "tache_terminee"
    return ("en_cours", None) if task_status == "à_faire" else (None, None)


def _sync_task_status(
    task_id: str, step_status: str, *, protocol_id: str = ""
) -> str:
    """Carry a step's new status to its linked task. Returns what happened.

    ``"synced"`` (the task was written, and its DAV collection bumped),
    ``"noop"`` (it already agreed), ``"skipped_cancelled"`` (a cancelled
    task, left alone — :func:`cascaded_task_status`), ``"skipped"`` (a done
    task an ``en_cours`` step would have downgraded), ``"missing"`` (the
    linked task was not found), ``"failed"`` (the write was refused or
    raised), ``"none"`` (re-entered through the sync guard).

    The task write compare-and-sets against the version this function just
    read: the decision above was made on it, and a write landing in between
    (the phone cancelling the task) must be decided again, not overwritten.
    One re-read is allowed; a second conflict reports ``"failed"``.

    The CTag bump lives HERE, not in a route — a documented in-model bump,
    like :func:`create_linked_tasks`. This write is made behind the
    caller's back on a DAV-exposed record: the web step route never bumped,
    so the phone never learned that the task was completed or reopened.
    Every caller of the cascade now bumps by construction. The bump is
    guarded on its own: the task write it follows has committed.

    Never raises: the step write it follows has committed. Failures are
    logged (ids only) and reported.
    """
    if task_id in _SYNCING:
        return "none"
    _SYNCING.add(task_id)
    try:
        from dav.sync import bump_ctag, collection_for
        from models import concurrency
        from models.task import get_task, update_task

        for _attempt in range(2):
            task = get_task(task_id)
            if task is None:
                log_protocol_event("cascade_task_skipped", protocol_id,
                                   outcome="refused",
                                   reason="tache_introuvable",
                                   task_id=task_id, step_status=step_status)
                return "missing"
            current = task.get("status", "")
            new_status, skip_reason = cascaded_task_status(step_status, current)
            if skip_reason is not None:
                log_protocol_event("cascade_task_skipped", protocol_id,
                                   reason=skip_reason, task_id=task_id,
                                   step_status=step_status,
                                   task_status=current)
                return ("skipped_cancelled" if skip_reason == "tache_annulee"
                        else "skipped")
            if new_status is None:
                return "noop"
            doc, errors = update_task(
                task_id, {"status": new_status},
                expected_etag=concurrency.etag_of(task),
            )
            if errors and concurrency.is_stale(errors):
                continue
            if errors or doc is None:
                log_protocol_event("cascade_task_skipped", protocol_id,
                                   outcome="refused",
                                   reason="ecriture_refusee",
                                   task_id=task_id, step_status=step_status)
                return "failed"
            try:
                bump_ctag(collection_for(doc.get("dossier_id")))
            except Exception:
                log_unexpected("protocol cascade: task CTag bump failed",
                               task_id=task_id)
            return "synced"
        log_protocol_event("cascade_task_skipped", protocol_id,
                           outcome="refused", reason="concurrence",
                           task_id=task_id, step_status=step_status)
        return "failed"
    except Exception:
        # Swallowed on purpose — the step write it follows has committed —
        # but never SILENTLY: a task left out of step with its step is a
        # data inconsistency, so it goes through the typed helper (plan
        # rule 14 — the raw logger.warning of the cascade is gone). Ids
        # only; the traceback is scrubbed by the RedactionFilter.
        log_unexpected("protocol cascade: task status sync failed",
                       task_id=task_id, step_status=step_status)
        return "failed"
    finally:
        _SYNCING.discard(task_id)


def _check_protocol_completion(protocol_id: str) -> bool:
    """Close an « actif » protocol whose steps are ALL complété (not only
    the mandatory ones). True when it closed.

    Decided and written in ONE transaction on the protocol and its steps as
    they stand at commit: a step reopened between the read and the write
    aborts it, and the retry leaves the protocol open — the old
    read-then-update could close a protocol over a step that had just
    reopened. The closure is stamped ``closed_by = CLOSED_BY_AUTO`` with
    ``closed_at``: it is the one closure the cascade may undo by itself
    (reopening a step or its linked task reactivates the protocol).
    """
    proto_ref = db.collection(COLLECTION).document(protocol_id)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def _close(txn) -> bool:
        snap = proto_ref.get(transaction=txn)
        if not snap.exists:
            return False
        if (snap.to_dict() or {}).get("status") != "actif":
            return False
        steps = [
            s.to_dict() or {}
            for s in proto_ref.collection(STEPS_SUBCOLLECTION).stream(
                transaction=txn)
        ]
        if not steps or not all(s.get("status") == "complété" for s in steps):
            return False
        txn.update(proto_ref, {
            "status": "complété",
            "closed_by": CLOSED_BY_AUTO,
            "closed_at": now,
            **provenance.update_fields(now),
        })
        return True

    try:
        closed = _close(db.transaction())
    except Exception:
        log_unexpected("protocol cascade: completion check failed",
                       protocol_id=protocol_id)
        return False
    if closed:
        provenance.note_commit(COLLECTION, protocol_id)
    return closed
