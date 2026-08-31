"""Répartiteur des tâches planifiées (Phase N §12) + livraison du rapport.

The 15-minute cron sweep: read the ACTIVE definitions (bounded, Python
due-filter — zero index), and per due occurrence run the all-in-one
dispatch transaction (models/chat_scheduled_task.dispatch_occurrence), then
enqueue the chain. Due ⇔ the recurrence matches TODAY (local Montréal
date), ``now.hour >= hour_local`` (a missed window self-heals later the
same day), and the occurrence is unmarked — the local-date key is what
makes DST a non-event (§12.1).

``jours_ouvrables`` = JURIDICAL days (weekday minus Québec statutory
holidays, ``utils.deadlines.is_juridical_day``) — the term already means
that everywhere in this codebase (``add_jours_ouvrables``, the
``3_jours_ouvrables`` avis key), the same French words meaning two things
would be a trap, and a morning briefing on Jour de l'An serves nobody.
One-line override if plain weekdays are ever wanted.

The REPAIR pass (the reconciliation doctrine — every repair is ERROR, it
must be SEEN): an occurrence marked today/yesterday whose chain never
started (pending assistant turn, continuation never enqueued, older than
the grace window) is re-enqueued with its STORED token. The dispatch
transaction guarantees a marked occurrence always HAS its conversation, so
un-enqueued is the only orphan class.

:func:`livrer_rapport` is the §12.4 delivery, called by the turn engine on
a scheduled conversation's FINAL commit: at-most-once via the transactional
marker (``poser_marqueur_courriel`` — the poser_accuse posture: once the
marker is set, a Graph failure is logged and never raised — a retry could
never resend, and the in-app copy remains, unread-marked).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

import bleach as _bleach
import markdown as _markdown_lib

from config import Config
from models import chat_conversation as conv_model
from models import chat_scheduled_task as task_model
from utils import courriel, deadlines
from utils.graph import GraphNotConfigured
from utils.logging_setup import log_chat_event, log_unexpected
from utils.markdown_docx import (
    ALLOWED_ATTRS,
    ALLOWED_TAGS,
    MD_EXTENSION_CONFIGS,
    MD_EXTENSIONS,
)

_MTL = ZoneInfo("America/Montreal")
# An occurrence marked but never enqueued is repaired after this grace —
# long enough for the nominal dispatch→enqueue gap, short enough that the
# 07:00 briefing still lands in the morning.
_REPAIR_GRACE = timedelta(minutes=5)


def _now_mtl() -> datetime:
    return datetime.now(_MTL)


@lru_cache(maxsize=1)
def _owner_uid() -> str:
    """The single authorized user's Firebase uid — the Storage tenant key
    of scheduled conversations. Loud failure: a dispatch without an owner
    would write offloaded blocks under an empty prefix."""
    from firebase_admin import auth as firebase_auth

    user = firebase_auth.get_user_by_email(Config.AUTHORIZED_USER_EMAIL)
    return user.uid


def est_due(task: dict, now_mtl: datetime) -> Optional[str]:
    """The occurrence date (local, ``YYYY-MM-DD``) this task is due for at
    *now*, or None."""
    local_date = now_mtl.date()
    recurrence = task.get("recurrence") or {}
    kind = recurrence.get("kind", "")
    if kind == "jours_ouvrables":
        if not deadlines.is_juridical_day(local_date):
            return None
    elif kind == "hebdomadaire":
        if local_date.weekday() != int(recurrence.get("day") or 0):
            return None
    elif kind != "quotidien":
        return None
    if now_mtl.hour < int(task.get("hour_local") or 0):
        return None
    occurrence = local_date.isoformat()
    if occurrence in (task.get("occurrences") or {}):
        return None
    return occurrence


def executer_balayage() -> dict:
    """One cron sweep. Returns the counters the summary log carries."""
    now = _now_mtl()
    tasks = task_model.list_active()
    dues = 0
    dispatched = 0
    skipped = 0
    repaired = 0
    for task in tasks:
        occurrence = est_due(task, now)
        if occurrence:
            dues += 1
            if _dispatch(task, occurrence):
                dispatched += 1
            else:
                skipped += 1
        repaired += _reparer(task, now)
    log_chat_event(
        "chat_scheduler_execute",
        dues=dues,
        dispatched=dispatched,
        skipped=skipped,
        repaired=repaired,
    )
    return {
        "dues": dues,
        "dispatched": dispatched,
        "skipped": skipped,
        "repaired": repaired,
    }


def _dispatch(task: dict, occurrence: str) -> bool:
    task_id = task.get("id", "")
    try:
        owner_uid = _owner_uid()
    except Exception:
        log_chat_event(
            "chat_scheduled_dispatch",
            "failure",
            task_id=task_id,
            occurrence=occurrence,
            reason="owner_uid_unresolved",
        )
        return False

    conversation, errors = conv_model.prepare_conversation(
        {
            "title": f"{task.get('name', '')} — {occurrence}",
            "model": task.get("model", Config.CHAT_DEFAULT_MODEL),
            "dossier_id": task.get("dossier_id", ""),
            "dossier_file_number": task.get("dossier_file_number", ""),
            "dossier_title": task.get("dossier_title", ""),
            "owner_uid": owner_uid,
            "skill_selection": task.get("skill_selection") or [],
            "origin": "planifiee",
            "scheduled_task_id": task_id,
            "unread": True,   # § 12.4 — lands in « Flottantes », marked new
        }
    )
    if errors:
        log_chat_event(
            "chat_scheduled_dispatch",
            "failure",
            task_id=task_id,
            occurrence=occurrence,
            reason="conversation_invalide",
        )
        return False
    user_turn, assistant_turn, errors = conv_model.prepare_turn_pair(
        task.get("prompt", ""), by="planificateur", addendum="unattended"
    )
    if errors:
        log_chat_event(
            "chat_scheduled_dispatch",
            "failure",
            task_id=task_id,
            occurrence=occurrence,
            reason="prompt_vide",
        )
        return False
    conversation["turn_count"] = assistant_turn["seq"]
    conversation["active_turn_id"] = assistant_turn["id"]

    try:
        outcome = task_model.dispatch_occurrence(
            task_id,
            occurrence,
            conversation=conversation,
            user_turn=user_turn,
            assistant_turn=assistant_turn,
        )
    except Exception:
        log_unexpected(
            "chat scheduled dispatch failed", task_id=task_id
        )
        return False
    if outcome != "dispatched":
        log_chat_event(
            "chat_scheduled_dispatch",
            "refused",
            task_id=task_id,
            occurrence=occurrence,
            reason=(
                "already_dispatched" if outcome == "already" else outcome
            ),
        )
        return False

    log_chat_event(
        "chat_scheduled_dispatch",
        task_id=task_id,
        occurrence=occurrence,
        conversation_id=conversation["id"],
    )
    _enfiler(conversation["id"], assistant_turn)
    return True


def _enfiler(conversation_id: str, assistant_turn: dict) -> None:
    from chat import taches

    token = (assistant_turn.get("continuation") or {}).get("token", "")
    try:
        taches.enfiler_tour(conversation_id, assistant_turn["id"], token)
        conv_model.mark_enqueued(conversation_id, assistant_turn["id"], token)
        log_chat_event(
            "chat_turn_started",
            conversation_id=conversation_id,
            turn_id=assistant_turn["id"],
            scheduled=True,
        )
    except Exception:
        # The occurrence is marked and its conversation exists — the repair
        # pass re-enqueues within the grace window. Loud, never silent.
        log_chat_event(
            "chat_enqueue_failed",
            "failure",
            conversation_id=conversation_id,
            turn_id=assistant_turn["id"],
        )


def _reparer(task: dict, now_mtl: datetime) -> int:
    """Re-enqueue today's/yesterday's marked occurrences whose chain never
    started. Returns how many were repaired (each is an ERROR log — the
    queue lost work and it must be SEEN)."""
    from chat import taches

    repaired = 0
    window = {
        now_mtl.date().isoformat(),
        (now_mtl.date() - timedelta(days=1)).isoformat(),
    }
    occurrences = task.get("occurrences") or {}
    for occurrence, entry in occurrences.items():
        if occurrence not in window:
            continue
        conversation_id = (entry or {}).get("conversation_id", "")
        if not conversation_id:
            continue
        conv = conv_model.get_conversation(conversation_id)
        if conv is None:
            continue
        turn_id = conv.get("active_turn_id") or ""
        if not turn_id:
            continue  # the chain ran (terminal commits clear the marker)
        turn = conv_model.get_turn(conversation_id, turn_id)
        # pending = the chain never started; running with an un-enqueued
        # continuation = a mid-chain enqueue failure whose queue retries
        # ran out. Both are the same orphan class: a rotated token nobody
        # will ever deliver.
        if turn is None or turn.get("state") not in ("pending", "running"):
            continue
        continuation = turn.get("continuation") or {}
        if continuation.get("enqueued") or not continuation.get("token"):
            continue
        stamp = turn.get("updated_at") or turn.get("created_at")
        if stamp is not None and hasattr(stamp, "timestamp"):
            # Age against the SWEEP's clock (the frozen-clock doctrine —
            # one clock per decision), never a second wall-clock read.
            age = now_mtl.timestamp() - stamp.timestamp()
            if age < _REPAIR_GRACE.total_seconds():
                continue
        try:
            taches.enfiler_tour(
                conversation_id, turn_id, continuation.get("token", "")
            )
            conv_model.mark_enqueued(
                conversation_id, turn_id, continuation.get("token", "")
            )
        except Exception:
            log_unexpected(
                "chat scheduled repair enqueue failed",
                conversation_id=conversation_id,
            )
            continue
        repaired += 1
        log_chat_event(
            "chat_scheduled_repair",
            "failure",   # ERROR by doctrine — the queue lost work
            task_id=task.get("id", ""),
            occurrence=occurrence,
            conversation_id=conversation_id,
        )
    return repaired


# ── §12.4 — deliver_email ───────────────────────────────────────────────────


def _render_markdown(text: str) -> str:
    """The screen's exact pipeline (main.py imports the same constants) —
    the emailed report renders as the transcript does."""
    html = _markdown_lib.markdown(
        text, extensions=MD_EXTENSIONS, extension_configs=MD_EXTENSION_CONFIGS
    )
    return _bleach.clean(
        html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True
    )


def _final_report(conversation_id: str) -> tuple[str, bool, bool]:
    """``(texte, conclu, lisible)`` du dernier tour d'assistant terminé.

    Trois valeurs plutôt qu'une chaîne, parce que « chaîne vide » recouvrait
    trois états qu'il faut distinguer avant de décider d'un envoi :

    ``lisible`` — ``list_turns`` échoue OUVERT (une panne Firestore rend une
    liste vide). Sans ce drapeau, une lecture ratée s'annonçait « rapport
    vide », c'est-à-dire une affirmation sur des données que personne n'avait
    lues — le motif que le dépôt interdit ailleurs (``get_dossier_strict``,
    ``subtree_members``).

    ``conclu`` — le DERNIER segment a-t-il porté du texte ? Le corps
    concatène tous les segments, si bien qu'une narration intercalaire
    (« Je consulte l'outil 1. ») suffisait à rendre le rapport « non vide »
    alors que le tour n'a jamais produit de conclusion. C'est la forme que
    produit une troncature : les appels d'outils narrés, puis plus rien.
    """
    try:
        turns = conv_model.list_turns(conversation_id)
    except Exception:
        return "", False, False
    if not turns:
        return "", False, False
    for turn in reversed(turns):
        if turn.get("role") == "assistant" and turn.get("state") == "final":
            parts: list[str] = []
            dernier: list[str] = []
            for segment in turn.get("segments") or []:
                dernier = []
                for block in segment.get("blocks") or []:
                    if block.get("type") == "text":
                        dernier.append(block.get("text", ""))
                    elif (
                        block.get("type") == "storage_ref"
                        and block.get("original_type") == "text"
                    ):
                        # Preview only — an emailed report is a convenience
                        # copy; the app holds the complete registre.
                        dernier.append(block.get("preview", ""))
                parts.extend(dernier)
            texte = "\n\n".join(p for p in parts if p)
            return texte, any(p.strip() for p in dernier), True
    return "", False, True


_MOTIFS_AVIS = {
    "registre_illisible": (
        "Le registre de la conversation n'a pas pu être lu, donc le rapport "
        "n'a pas pu être assemblé. Rien n'indique que le travail lui-même a "
        "échoué : ouvrez la conversation dans Athéna."
    ),
    "aucun_rapport": (
        "L'exécution s'est terminée sans produire de rapport. Les outils "
        "ont pu s'exécuter — ouvrez la conversation dans Athéna pour voir "
        "où elle s'est arrêtée."
    ),
    "echec": (
        "L'exécution a échoué avant de produire un rapport. Ouvrez la "
        "conversation dans Athéna : le dernier tour porte l'erreur."
    ),
}


def _livrer_avis(conv: dict, task_id: str, motif: str) -> None:
    """Dire à l'avocat que le rapport n'aura pas lieu — sur le canal où il
    attend le rapport.

    Ne rien envoyer était le premier réflexe, et c'était une erreur : ce
    défaut n'a été DÉTECTÉ que parce qu'un courriel vide est arrivé. Une
    absence, un mardi matin, n'est pas un événement — elle se confond avec
    « rien à signaler », et la tâche peut mourir des semaines sans que
    personne s'en aperçoive. Un courriel vide entraîne à ignorer le canal ;
    le silence le supprime. L'avis nomme la panne et renvoie à la
    conversation.

    Il emprunte le MÊME marqueur que le rapport : au plus un courriel par
    exécution, avis compris, et jamais les deux.
    """
    conversation_id = conv.get("id", "")
    if not conv_model.poser_marqueur_courriel(conversation_id):
        return
    try:
        courriel.envoyer(
            Config.AUTHORIZED_USER_EMAIL,
            f"Rapport planifié — échec — {conv.get('title', '')}",
            f"<p>{_MOTIFS_AVIS.get(motif, _MOTIFS_AVIS['echec'])}</p>",
        )
        log_chat_event(
            "chat_report_emailed", "failure",
            task_id=task_id, conversation_id=conversation_id, reason=motif,
        )
    except Exception:
        log_chat_event(
            "chat_report_emailed", "failure",
            task_id=task_id, conversation_id=conversation_id,
            reason="avis_non_livre",
        )


def livrer_echec(conv: Optional[dict]) -> None:
    """L'avis, appelé depuis les branches d'ÉCHEC du moteur.

    ``livrer_rapport`` ne s'atteint que sur la branche de succès terminal :
    un tour mort d'épuisement des reprises ou d'une erreur fatale n'y passe
    jamais, donc il ne livrait rien du tout.
    """
    if not conv or conv.get("origin") != "planifiee":
        return
    task_id = conv.get("scheduled_task_id") or ""
    task = task_model.get_task(task_id) if task_id else None
    if not task or not task.get("deliver_email"):
        return
    _livrer_avis(conv, task_id, "echec")


def livrer_rapport(conv: dict) -> None:
    """Send the finalized report by email when the task asks for it.

    Called by the turn engine AFTER the final commit; every failure path is
    logged and swallowed — a delivery problem must never fail a committed
    turn (the in-app copy remains, unread-marked)."""
    if conv.get("origin") != "planifiee":
        return
    task_id = conv.get("scheduled_task_id") or ""
    task = task_model.get_task(task_id) if task_id else None
    if not task or not task.get("deliver_email"):
        return
    conversation_id = conv.get("id", "")
    # Les deux vérifications passent AVANT le marqueur. Le poser d'abord
    # brûlerait le jeton d'au-plus-une-fois sur un envoi qui n'a jamais eu
    # lieu, et plus rien n'aurait pu livrer ensuite — même une fois le
    # défaut corrigé.
    rapport, conclu, lisible = _final_report(conversation_id)
    if not lisible:
        _livrer_avis(conv, task_id, "registre_illisible")
        return
    if not rapport.strip() or not conclu:
        _livrer_avis(conv, task_id, "aucun_rapport")
        return
    if not conv_model.poser_marqueur_courriel(conversation_id):
        return  # already sent (or being sent by a racing finalizer)
    try:
        corps = _render_markdown(rapport)
        courriel.envoyer(
            Config.AUTHORIZED_USER_EMAIL,
            f"Rapport planifié — {conv.get('title', '')}",
            corps,
        )
        log_chat_event(
            "chat_report_emailed",
            task_id=task_id,
            conversation_id=conversation_id,
        )
    except GraphNotConfigured:
        log_chat_event(
            "chat_report_emailed",
            "refused",
            task_id=task_id,
            conversation_id=conversation_id,
            reason="graph_not_configured",
        )
    except Exception:
        # GraphError included. The marker is set: a retry could never
        # resend. Logged, never raised — the accusé posture.
        log_chat_event(
            "chat_report_emailed",
            "failure",
            task_id=task_id,
            conversation_id=conversation_id,
            reason="graph_error",
        )
