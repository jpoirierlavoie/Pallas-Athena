"""Conversations du clavardage (Phase N) — le registre et ses gardes.

``chat_conversations/{id}`` is Rule 7 complete (this doc IS mutated:
counters, the in-flight marker, the unread flag). Its ``turns/{seq:06d}``
subcollection is the REGISTRE (SPEC §6.2): one document per message event,
zero-padded ids so ``__name__`` order is chronological with no index.

**Rule-7 EXCEPTION (documented, the audit_events family):** turn documents
carry NO ``etag`` — the ``step_token`` is THE concurrency guard (an etag
would be a second, redundant token); user turns are write-once; an
assistant turn is mutable ONLY while non-terminal, and exclusively through
the transactional primitives below. Nothing here — or anywhere — deletes a
turn.

The at-least-once discipline (SPEC §2.2, the claim → work → commit shape):

* :func:`claim_step` — duplicate deliveries observe the advanced state and
  exit WITHOUT a model call; a rotated-but-never-enqueued continuation is
  repaired (the crash-between-commit-and-enqueue gap).
* :func:`commit_step` — re-reads and verifies the step token; a lost race
  discards the loser's result (logged by the engine, never silently
  double-recorded). Terminal commits fold the ACCOUNTING into the same
  transaction: conversation counters, the per-dossier roll-up
  (``chat_usage_dossier/{dossier_id}`` — counters-only doc, Rule-7
  exception, no etag), and the scheduled task's totals — Python sums over
  the turn's own segments, NEVER a Firestore aggregation (the June-2026
  index piège).

Honest accounting restatement (the SPEC's « tokens spent are tokens
recorded, always » is unattainable under at-least-once): a crash between
the Vertex response and the commit loses that call's usage record and the
retry re-pays the call. ``vertex_calls_started`` (stamped transactionally
at claim) vs ``vertex_calls_recorded`` (at commit) makes the drift itself
a recorded, queryable figure.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from google.cloud import firestore

from config import Config
from models import db
from security import sanitize
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

COLLECTION = "chat_conversations"
TURNS_SUBCOLLECTION = "turns"
USAGE_COLLECTION = "chat_usage_dossier"
TASKS_COLLECTION = "chat_scheduled_tasks"

VALID_ORIGINS = ("interactive", "planifiee")
TURN_STATES = ("pending", "running", "awaiting_authorization", "final", "failed")

TITLE_MAX_LENGTH = 200
_FIELD_MAX_LENGTH = 2000

_ZERO_TOTALS = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "web_search_requests": 0,
    "model_calls": 0,
}


def _seq_id(seq: int) -> str:
    return f"{seq:06d}"


def _conv_ref(conversation_id: str):
    return db.collection(COLLECTION).document(conversation_id)


def _turn_ref(conversation_id: str, turn_id: str):
    return _conv_ref(conversation_id).collection(TURNS_SUBCOLLECTION).document(turn_id)


# ── Pure helpers ────────────────────────────────────────────────────────────


def sum_segments(segments: list[dict]) -> dict:
    """Python sum of a turn's per-segment usage + cost. Pure — the terminal
    transaction calls it on data already read; no aggregation query."""
    totals = dict(_ZERO_TOTALS)
    usd_micros = 0
    for segment in segments or []:
        usage = segment.get("usage") or {}
        if usage:
            totals["model_calls"] += 1
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            totals[key] += int(usage.get(key) or 0)
        totals["web_search_requests"] += int(
            (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
        )
        usd_micros += int((segment.get("pricing") or {}).get("usd_micros") or 0)
    totals["usd_micros"] = usd_micros
    return totals


# ── Conversation CRUD ───────────────────────────────────────────────────────


def _sanitize_data(data: dict) -> dict:
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            limit = TITLE_MAX_LENGTH if key == "title" else _FIELD_MAX_LENGTH
            out[key] = sanitize(val, max_length=limit)
        else:
            out[key] = val
    return out


def prepare_conversation(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate and fill a conversation doc WITHOUT writing it — the shared
    builder of :func:`create_conversation` and the scheduled dispatcher's
    all-in-one transaction (models/chat_scheduled_task.dispatch_occurrence).
    """
    merged = {**_default_conversation(), **_sanitize_data(data)}
    errors: list[str] = []
    if not merged.get("title", "").strip():
        errors.append("Le titre de la conversation est requis.")
    if merged.get("model") not in Config.CHAT_MODELS:
        errors.append("Modèle inconnu — l'allowlist est fermée.")
    if merged.get("origin") not in VALID_ORIGINS:
        errors.append("Origine de conversation invalide.")
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    conversation_id = str(uuid.uuid4())
    merged.update(
        {
            "id": conversation_id,
            "skill_selection": [
                str(s) for s in (merged.get("skill_selection") or [])
            ],
            "created_at": now,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        }
    )
    return merged, []


def prepare_turn_pair(
    user_text: str,
    *,
    by: str = "juriste",
    addendum: str = "",
    base_seq: int = 0,
) -> tuple[Optional[dict], Optional[dict], list[str]]:
    """Build (user_turn, assistant_turn) docs WITHOUT writing them — the
    shape lives in exactly one place for the two writers (start_turn's
    transaction and the scheduled dispatcher's)."""
    text = sanitize(user_text or "", max_length=Config.CHAT_MESSAGE_MAX_CHARS)
    if not text.strip():
        return None, None, ["Le message est vide."]
    now = datetime.now(timezone.utc)
    seq_user = int(base_seq) + 1
    seq_assistant = seq_user + 1
    token = str(uuid.uuid4())
    user_turn = {
        "id": _seq_id(seq_user),
        "seq": seq_user,
        "role": "user",
        "content": [{"type": "text", "text": text}],
        "by": by,
        "created_at": now,
    }
    assistant_turn = {
        "id": _seq_id(seq_assistant),
        "seq": seq_assistant,
        "role": "assistant",
        "state": "pending",
        "step": 0,
        "step_token": token,
        "continuation": {"token": token, "enqueued": False},
        "segments": [],
        "authorization": None,
        "skill_versions": [],
        "charter_version": None,
        "addendum": addendum or "",
        "vertex_calls_started": 0,
        "vertex_calls_recorded": 0,
        "error": None,
        "truncated": False,
        "finalized_at": None,
        "created_at": now,
        "updated_at": now,
    }
    return user_turn, assistant_turn, []


def create_conversation(data: dict) -> tuple[Optional[dict], list[str]]:
    """Create a conversation shell (no turns yet). The CALLER resolves and
    denormalizes the dossier (and refuses an unresolvable id — the
    create_note doctrine); ``dossier_id`` is IMMUTABLE afterwards."""
    merged, errors = prepare_conversation(data)
    if errors:
        return None, errors
    try:
        _conv_ref(merged["id"]).set(merged)
    except Exception:
        log_unexpected("chat conversation create failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


def _default_conversation() -> dict:
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "owner_uid": "",
        "title": "",
        "model": Config.CHAT_DEFAULT_MODEL,
        "skill_selection": [],
        "status": "active",
        "origin": "interactive",
        "scheduled_task_id": "",
        "unread": False,
        "turn_count": 0,
        "active_turn_id": "",
        "token_totals": dict(_ZERO_TOTALS),
        "cost_snapshot": {"usd_micros_total": 0, "pricing_version": ""},
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def get_conversation(conversation_id: str) -> Optional[dict]:
    try:
        doc = _conv_ref(conversation_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning(
            "get_conversation failed for %s: %s",
            sanitize_log_value(conversation_id),
            exc,
        )
    return None


def list_conversations(limit: int = 200) -> list[dict]:
    """Bounded global read, newest-updated first — the UI groups by dossier
    in Python (ZERO composite index, the house doctrine). Fails open."""
    try:
        query = (
            db.collection(COLLECTION)
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
            .limit(max(1, int(limit)))
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.warning("list_conversations failed", exc_info=True)
        return []


def set_skill_selection(conversation_id: str, skills: list[str]) -> None:
    """Change the conversation's skill selection — effective at the NEXT
    turn (§5; each turn records the exact versions it used regardless)."""
    try:
        _conv_ref(conversation_id).update(
            {
                "skill_selection": [str(s) for s in (skills or [])],
                "updated_at": datetime.now(timezone.utc),
                "etag": str(uuid.uuid4()),
            }
        )
    except Exception:
        logger.warning(
            "set_skill_selection failed for %s",
            sanitize_log_value(conversation_id),
        )


def mark_read(conversation_id: str) -> None:
    """Clear the unread marker — idempotent, best-effort (a display aid)."""
    try:
        doc = get_conversation(conversation_id)
        if doc and doc.get("unread"):
            _conv_ref(conversation_id).update(
                {"unread": False, "etag": str(uuid.uuid4())}
            )
    except Exception:
        logger.warning(
            "mark_read failed for %s", sanitize_log_value(conversation_id)
        )


# ── Turns ───────────────────────────────────────────────────────────────────


def get_turn(conversation_id: str, turn_id: str) -> Optional[dict]:
    try:
        doc = _turn_ref(conversation_id, turn_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning(
            "get_turn failed for %s/%s: %s",
            sanitize_log_value(conversation_id),
            sanitize_log_value(turn_id),
            exc,
        )
    return None


def list_turns(conversation_id: str, limit: int = 500) -> list[dict]:
    """Chronological (zero-padded doc ids → ``__name__`` order). Bounded;
    the caller surfaces the truncation loudly. Fails open to []."""
    try:
        query = (
            _conv_ref(conversation_id)
            .collection(TURNS_SUBCOLLECTION)
            .order_by("seq")
            .limit(max(1, int(limit)))
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.warning(
            "list_turns failed for %s", sanitize_log_value(conversation_id)
        )
        return []


def start_turn(
    conversation_id: str,
    user_text: str,
    *,
    by: str = "juriste",
    addendum: str = "",
) -> tuple[Optional[dict], list[str]]:
    """Append the user turn + the pending assistant turn, transactionally.

    Refuses (French) while another assistant turn is in flight — ONE chain
    per conversation at a time; the ``active_turn_id`` marker on the
    conversation doc is maintained by the same transactions that move turn
    state, so the check and the marker can never disagree.

    Returns the ASSISTANT turn doc; the caller enqueues its first task with
    ``continuation.token`` and then best-effort :func:`mark_enqueued`.
    """
    conv_ref = _conv_ref(conversation_id)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn) -> tuple[Optional[dict], list[str]]:
        snap = conv_ref.get(transaction=txn)
        if not snap.exists:
            return None, ["Conversation introuvable."]
        conv = snap.to_dict()
        if conv.get("active_turn_id"):
            return None, [
                "Un tour est déjà en cours dans cette conversation. "
                "Attendez sa fin avant d'envoyer un nouveau message."
            ]
        user_turn, assistant_turn, errors = prepare_turn_pair(
            user_text,
            by=by,
            addendum=addendum,
            base_seq=int(conv.get("turn_count") or 0),
        )
        if errors:
            return None, errors
        txn.set(_turn_ref(conversation_id, user_turn["id"]), user_turn)
        txn.set(_turn_ref(conversation_id, assistant_turn["id"]), assistant_turn)
        txn.update(
            conv_ref,
            {
                "turn_count": assistant_turn["seq"],
                "active_turn_id": assistant_turn["id"],
                "updated_at": assistant_turn["created_at"],
                "etag": str(uuid.uuid4()),
            },
        )
        return assistant_turn, []

    try:
        return _txn(transaction)
    except Exception:
        log_unexpected("chat start_turn failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]


def mark_enqueued(conversation_id: str, turn_id: str, token: str) -> None:
    """Best-effort ``continuation.enqueued = True`` — the crash-repair flag.

    Only when the stored continuation still carries *token*: a stale caller
    must never mark a NEWER continuation as enqueued (that would disarm the
    repair branch for the step that actually needs it)."""
    try:
        turn = get_turn(conversation_id, turn_id)
        continuation = (turn or {}).get("continuation") or {}
        if continuation.get("token") == token:
            _turn_ref(conversation_id, turn_id).update(
                {"continuation": {"token": token, "enqueued": True}}
            )
    except Exception:
        logger.warning(
            "mark_enqueued failed for %s/%s",
            sanitize_log_value(conversation_id),
            sanitize_log_value(turn_id),
        )


def claim_step(
    conversation_id: str, turn_id: str, step_token: str
) -> tuple[str, Optional[dict], Optional[str]]:
    """The claim transaction — ``("proceed", turn, None)`` |
    ``("skip", None, None)`` | ``("repair", None, token_to_reenqueue)``.

    Deliberate non-consumption of the token: the retry of a task that
    crashed MID-CALL is the same delivery with the same token and must be
    able to redo the call. Two truly concurrent deliveries can therefore
    both proceed and double-pay one model call — commit_step detects the
    race and discards the loser; the queue's max-concurrent-dispatches=2
    keeps that window near zero.
    """
    ref = _turn_ref(conversation_id, turn_id)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn) -> tuple[str, Optional[dict], Optional[str]]:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return "skip", None, None
        turn = snap.to_dict()
        state = turn.get("state")
        if state in ("final", "failed"):
            return "skip", None, None
        if state == "awaiting_authorization":
            return "skip", None, None
        if turn.get("step_token") != step_token:
            continuation = turn.get("continuation") or {}
            if continuation.get("token") and not continuation.get("enqueued"):
                return "repair", None, continuation["token"]
            return "skip", None, None
        started = int(turn.get("vertex_calls_started") or 0) + 1
        txn.update(
            ref,
            {
                "state": "running",
                "vertex_calls_started": started,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        turn["state"] = "running"
        turn["vertex_calls_started"] = started
        return "proceed", turn, None

    return _txn(transaction)


def commit_step(
    conversation_id: str,
    turn_id: str,
    step_token: str,
    *,
    next_state: str,
    segment: Optional[dict] = None,
    last_segment_tool_results: Optional[list] = None,
    authorization: Optional[dict] = None,
    stamps: Optional[dict] = None,
    error: Optional[dict] = None,
    truncated: bool = False,
) -> tuple[str, Optional[str]]:
    """The commit transaction — ``("committed", new_token)`` or
    ``("lost_race", None)``.

    * ``segment`` is appended (read-modify-write of the list — bounded by
      the offload budget, and array-of-array-safe).
    * ``last_segment_tool_results`` attaches results to the PREVIOUS
      segment (the authorization-resume path).
    * ``next_state == "running"`` mints a continuation for the caller to
      enqueue; ``awaiting_authorization`` rotates the token with NO
      continuation (the pause is « no task exists »); terminal states fold
      the accounting in (conversation counters + dossier roll-up +
      scheduled-task totals), all reads before writes.
    """
    if next_state not in TURN_STATES:
        raise ValueError(f"invalid next_state: {next_state}")
    turn_ref = _turn_ref(conversation_id, turn_id)
    conv_ref = _conv_ref(conversation_id)
    terminal = next_state in ("final", "failed")
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn) -> tuple[str, Optional[str]]:
        snap = turn_ref.get(transaction=txn)
        if not snap.exists:
            return "lost_race", None
        turn = snap.to_dict()
        if turn.get("step_token") != step_token:
            return "lost_race", None

        # ALL READS FIRST (Firestore transaction rule).
        conv_snap = conv_ref.get(transaction=txn)
        conv = conv_snap.to_dict() if conv_snap.exists else None
        usage_ref = usage_snap = None
        task_ref = task_snap = None
        if terminal and conv:
            dossier_id = conv.get("dossier_id") or ""
            if dossier_id:
                usage_ref = db.collection(USAGE_COLLECTION).document(dossier_id)
                usage_snap = usage_ref.get(transaction=txn)
            task_id = conv.get("scheduled_task_id") or ""
            if task_id:
                task_ref = db.collection(TASKS_COLLECTION).document(task_id)
                task_snap = task_ref.get(transaction=txn)

        now = datetime.now(timezone.utc)
        segments = list(turn.get("segments") or [])
        if last_segment_tool_results is not None and segments:
            segments[-1] = {
                **segments[-1],
                "tool_results": last_segment_tool_results,
            }
        if segment is not None:
            segments.append(segment)

        new_token = str(uuid.uuid4())
        updates: dict[str, Any] = {
            "segments": segments,
            "state": next_state,
            "step": int(turn.get("step") or 0) + (1 if segment else 0),
            "step_token": new_token,
            "updated_at": now,
        }
        if segment is not None and segment.get("usage"):
            updates["vertex_calls_recorded"] = (
                int(turn.get("vertex_calls_recorded") or 0) + 1
            )
        if stamps:
            updates.update(stamps)
        if authorization is not None:
            updates["authorization"] = authorization
        if next_state == "running":
            updates["continuation"] = {"token": new_token, "enqueued": False}
        else:
            updates["continuation"] = None
        if terminal:
            updates["finalized_at"] = now
            updates["truncated"] = bool(turn.get("truncated")) or truncated
            if error is not None:
                updates["error"] = error
        txn.update(turn_ref, updates)

        if terminal and conv is not None:
            totals = sum_segments(segments)
            conv_totals = dict(conv.get("token_totals") or _ZERO_TOTALS)
            for key in _ZERO_TOTALS:
                conv_totals[key] = int(conv_totals.get(key) or 0) + totals[key]
            cost = dict(conv.get("cost_snapshot") or {})
            cost["usd_micros_total"] = (
                int(cost.get("usd_micros_total") or 0) + totals["usd_micros"]
            )
            cost["pricing_version"] = Config.CHAT_PRICING.get("version", "")
            txn.update(
                conv_ref,
                {
                    "active_turn_id": "",
                    "token_totals": conv_totals,
                    "cost_snapshot": cost,
                    "updated_at": now,
                    "etag": str(uuid.uuid4()),
                },
            )
            if usage_ref is not None:
                _apply_usage(txn, usage_ref, usage_snap, conv, totals, now)
            if task_ref is not None and task_snap is not None and task_snap.exists:
                task = task_snap.to_dict()
                task_totals = dict(task.get("usage_totals") or _ZERO_TOTALS)
                for key in _ZERO_TOTALS:
                    task_totals[key] = (
                        int(task_totals.get(key) or 0) + totals[key]
                    )
                usd = int(task.get("usd_micros_total") or 0) + totals["usd_micros"]
                txn.update(
                    task_ref,
                    {
                        "usage_totals": task_totals,
                        "usd_micros_total": usd,
                        "updated_at": now,
                    },
                )
        return "committed", new_token

    return _txn(transaction)


def _apply_usage(txn, usage_ref, usage_snap, conv: dict, totals: dict, now) -> None:
    """Increment (or create) the per-dossier roll-up — counters only."""
    if usage_snap is not None and usage_snap.exists:
        doc = usage_snap.to_dict()
        doc_totals = dict(doc.get("token_totals") or _ZERO_TOTALS)
        for key in _ZERO_TOTALS:
            doc_totals[key] = int(doc_totals.get(key) or 0) + totals[key]
        txn.update(
            usage_ref,
            {
                "token_totals": doc_totals,
                "usd_micros_total": (
                    int(doc.get("usd_micros_total") or 0) + totals["usd_micros"]
                ),
                "updated_at": now,
            },
        )
    else:
        txn.set(
            usage_ref,
            {
                "dossier_id": conv.get("dossier_id", ""),
                "token_totals": {
                    key: totals[key] for key in _ZERO_TOTALS
                },
                "usd_micros_total": totals["usd_micros"],
                "created_at": now,
                "updated_at": now,
            },
        )


def fail_turn(
    conversation_id: str,
    turn_id: str,
    *,
    reason: str,
    error: Optional[dict] = None,
) -> bool:
    """Terminalize a turn as ``failed`` REGARDLESS of the step token — the
    retry-exhaustion path (the queue is done redelivering; whatever token
    the last task carried, the turn must stop reading « pending » for
    ever). Idempotent: an already-terminal turn is left untouched.
    """
    turn = get_turn(conversation_id, turn_id)
    if turn is None:
        return False
    if turn.get("state") in ("final", "failed"):
        return True
    status, _token = commit_step(
        conversation_id,
        turn_id,
        turn.get("step_token", ""),
        next_state="failed",
        error={"code": reason, **(error or {})},
    )
    return status == "committed"


def poser_marqueur_courriel(conversation_id: str) -> bool:
    """Transactional test-and-set on ``courriel_livre`` — the poser_accuse
    doctrine verbatim: True exactly ONCE per conversation, the single guard
    of the single non-idempotent effect (the deliver_email send, SPEC
    §12.4). Any failure → False (fail closed: better a missing email — the
    in-app copy remains, unread-marked — than a duplicate)."""
    ref = _conv_ref(conversation_id)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return False
        if snap.to_dict().get("courriel_livre"):
            return False
        txn.update(
            ref,
            {
                "courriel_livre": True,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        return True

    try:
        return _txn(transaction)
    except Exception:
        logger.warning(
            "poser_marqueur_courriel failed for %s",
            sanitize_log_value(conversation_id),
        )
        return False


def decide_authorization(
    conversation_id: str,
    turn_id: str,
    *,
    approved: list[str],
    refused: list[str],
) -> tuple[str, Optional[str]]:
    """Record the lawyer's decision and re-arm the chain —
    ``("ok", new_token)`` | ``("invalid", None)``.

    Valid only from ``awaiting_authorization`` with no decision yet: a
    second POST (double-click, stale tab) is refused rather than
    re-deciding. The caller enqueues the returned token."""
    ref = _turn_ref(conversation_id, turn_id)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn) -> tuple[str, Optional[str]]:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return "invalid", None
        turn = snap.to_dict()
        if turn.get("state") != "awaiting_authorization":
            return "invalid", None
        authorization = dict(turn.get("authorization") or {})
        if authorization.get("decision"):
            return "invalid", None
        now = datetime.now(timezone.utc)
        authorization["decision"] = {
            "approved": list(approved),
            "refused": list(refused),
            "decided_at": now,
        }
        token = str(uuid.uuid4())
        txn.update(
            ref,
            {
                "authorization": authorization,
                "state": "running",
                "step_token": token,
                "continuation": {"token": token, "enqueued": False},
                "updated_at": now,
            },
        )
        return "ok", token

    return _txn(transaction)
