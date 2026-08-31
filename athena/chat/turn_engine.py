"""L'orchestrateur de tour (Phase N) — claim → work → commit.

One worker task = at most ONE Vertex model call (SPEC §2.2), plus whatever
tool work the previous call requested. The transactional guards live in
``models/chat_conversation.py``; this module owns assembly, the Vertex
call, tool execution, the offload/rehydration policy, and the outcome
taxonomy the machine route maps onto HTTP statuses:

* ``ChatVertexRetryable`` propagates — the route answers 5xx and Cloud
  Tasks retries on the queue's backoff (enqueue failures ride the same
  class: the retry lands in the claim's REPAIR branch).
* everything else terminalizes the turn LOUDLY (``failed`` + reason) and
  answers 200 — retrying a deterministic failure would re-pay expensive
  model calls to reproduce it (the taches_portail malformed-payload
  doctrine, priced in tokens).

VERBATIM rule (the review's hard finding): thinking blocks carry
``signature``s and web_search results carry ``encrypted_content`` that must
be replayed BYTE-EXACT or Vertex refuses the continuation with a 400.
Assembly therefore always rehydrates ``storage_ref`` blocks from Storage
(sha256-verified — a mismatch fails the turn, never a silent divergence);
the inline ``preview`` is UI-only and never enters a request.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from firebase_admin import storage

from config import Config
from models import chat_charter as charter_model
from models import chat_conversation as conv_model
from models import chat_skill as skill_model
from models import document as document_model
from utils import graph_messagerie
from utils.logging_setup import log_chat_event, log_unexpected
from utils.tracing_setup import span

from chat import charter, executors, registry, taches, vertex

# The native-PDF fallback (decision D2): when get_document_text reports a
# fully scanned window, the engine attaches the document itself as a
# base64 `document` block so the model reads it visually. Bounded well
# under the API's request ceiling (base64 inflates ~4/3) and under the
# native-PDF 100-page limit.
_NATIVE_PDF_MAX_BYTES = 20 * 1024 * 1024
_NATIVE_PDF_MAX_PAGES = 100

_REFUSED_BY_LAWYER_FR = "Refusé par l'avocat."


class _StorageRefCorrupt(Exception):
    pass


# ── Storage offload / rehydration ───────────────────────────────────────────


def _serialize_block(block: dict) -> str:
    return json.dumps(block, ensure_ascii=False)


def _offload_block(block: dict, raw: str, conv: dict, turn: dict) -> dict:
    path = (
        f"users/{conv.get('owner_uid', '')}/chat/{conv['id']}/"
        f"{turn['id']}/{uuid.uuid4().hex}.json"
    )
    data = raw.encode("utf-8")
    # Upload BEFORE the Firestore commit that references it — a pointer
    # never dangles; an orphaned object from a lost race is inert.
    storage.bucket().blob(path).upload_from_string(
        data, content_type="application/json"
    )
    digest = hashlib.sha256(data).hexdigest()
    log_chat_event(
        "chat_block_offloaded",
        conversation_id=conv["id"],
        turn_id=turn["id"],
        size_bytes=len(data),
        original_type=str(block.get("type", "")),
    )
    return {
        "type": "storage_ref",
        "original_type": str(block.get("type", "")),
        "path": path,
        "size_bytes": len(data),
        "sha256": digest,
        # UI-only. NEVER enters an API request — assembly rehydrates.
        "preview": raw[:400],
    }


def _store_blocks(blocks: list[dict], conv: dict, turn: dict) -> list[dict]:
    """Offload any block whose serialization exceeds the threshold, plus
    the budget belt: the largest remaining inline blocks offload until the
    lot fits the turn-doc budget (Firestore's 1 MiB ceiling, with margin
    for the segments already committed)."""
    sized: list[tuple[int, str, dict]] = []
    for block in blocks:
        raw = _serialize_block(block)
        sized.append((len(raw.encode("utf-8")), raw, block))

    threshold = Config.CHAT_BLOCK_OFFLOAD_BYTES
    total_inline = sum(s for s, _r, b in sized if s <= threshold)
    budget = Config.CHAT_TURN_DOC_BUDGET_BYTES // 2  # margin for history
    force: set[int] = set()
    if total_inline > budget:
        for index in sorted(
            range(len(sized)), key=lambda i: sized[i][0], reverse=True
        ):
            size = sized[index][0]
            if size <= threshold and total_inline > budget:
                force.add(index)
                total_inline -= size

    out: list[dict] = []
    for index, (size, raw, block) in enumerate(sized):
        if block.get("type") == "storage_ref":
            out.append(block)
        elif size > threshold or index in force:
            out.append(_offload_block(block, raw, conv, turn))
        else:
            out.append(block)
    return out


def _rehydrate_block(block: dict) -> dict:
    if block.get("type") != "storage_ref":
        return block
    raw = storage.bucket().blob(block["path"]).download_as_bytes()
    if hashlib.sha256(raw).hexdigest() != block.get("sha256"):
        raise _StorageRefCorrupt(block.get("path", ""))
    return json.loads(raw.decode("utf-8"))


def _rehydrate_all(blocks: list[dict]) -> list[dict]:
    return [_rehydrate_block(b) for b in blocks or []]


# ── Assembly ────────────────────────────────────────────────────────────────


def _assemble_messages(turns: list[dict], current_turn_id: str) -> list[dict]:
    """The Messages array: prior FINAL turns verbatim (thinking included —
    signatures must survive), failed/pending assistant turns skipped, then
    the current turn's own segments and tool phases."""
    messages: list[dict] = []
    for turn in turns:
        role = turn.get("role")
        if role == "user":
            messages.append(
                {"role": "user", "content": _rehydrate_all(turn.get("content"))}
            )
            continue
        if role != "assistant":
            continue
        is_current = turn.get("id") == current_turn_id
        if not is_current and turn.get("state") != "final":
            continue  # a failed chain is not replayable history
        for segment in turn.get("segments") or []:
            messages.append(
                {
                    "role": "assistant",
                    "content": _rehydrate_all(segment.get("blocks")),
                }
            )
            tool_results = segment.get("tool_results")
            if tool_results:
                messages.append(
                    {"role": "user", "content": _rehydrate_all(tool_results)}
                )
    return messages


def _resolve_skills(conv: dict, turn: dict) -> tuple[list[dict], list[dict]]:
    """Les compétences de CE tour, et les versions à épingler.

    Même règle que la charte : **au pas 1** les têtes, **aux pas ≥ 2** les
    versions ÉPINGLÉES sur le tour. Sans cela le corps d'une compétence
    révisée en cours de chaîne changeait le prompt sans changer
    l'estampille — le registre affirmait alors une version que le modèle
    n'avait pas vue. Le fichier de référence, lui, se résolvait DÉJÀ à la
    paire épinglée : les deux moitiés d'une même compétence pouvaient donc
    venir de deux versions différentes dans le même tour.

    Une version épinglée illisible lève un RETRYABLE, là où ``get_heads``
    échoue OUVERT. Ce n'est pas une incohérence : dégrader est honnête à
    la PREMIÈRE résolution, puisque l'estampille enregistre ensuite ce qui
    a réellement été chargé. Au pas 2 l'estampille est déjà posée — sauter
    une compétence ferait mentir le registre, et un document de version
    est write-once : son absence est une anomalie, pas un état.

    ⚠ Les versions sont des DICTS, jamais des paires ``[id, version]`` :
    **Firestore refuse un tableau qui contient un tableau**, et une liste
    de paires est exactement cela. Écrite ainsi, elle a fait échouer le
    tout premier vrai tour du clavardage — `INVALID_ARGUMENT: Nested
    arrays are not allowed` — sur le commit, donc APRÈS l'appel de modèle,
    déjà payé. Les deux faux Firestore de la suite acceptaient la forme
    fautive ; ils modélisent la contrainte depuis.
    """
    epingles = turn.get("skill_versions") or []
    if epingles:
        heads: list[dict] = []
        for paire in epingles:
            if not isinstance(paire, dict):
                continue
            skill_id = str(paire.get("skill_id", ""))
            doc = skill_model.get_version(
                skill_id, int(paire.get("version") or 0)
            )
            if doc is None:
                raise vertex.ChatVertexRetryable("skill_version_unreadable")
            # `_version_doc` ne porte NI `id` NI `current_version` ; l'un
            # sert au tri stable de l'assemblage, l'autre au listing des
            # fichiers. Réinjectés depuis la paire, jamais relus.
            heads.append(
                {**doc, "id": skill_id, "current_version": doc.get("version")}
            )
        return heads, list(epingles)

    heads = skill_model.get_heads(conv.get("skill_selection") or [])
    versions = [
        {"skill_id": h.get("id", ""), "version": int(h.get("current_version") or 0)}
        for h in heads
    ]
    return heads, versions


def _resolve_charter(turn: dict) -> tuple[dict, int, str]:
    """La charte de CE tour → ``(résolue, version, source)``.

    **Au pas 1** on lit la tête. **Aux pas ≥ 2** — et à la reprise d'une
    pause d'autorisation — on relit la version ÉPINGLÉE sur le tour, jamais
    la tête : une charte révisée pendant que l'avocat délibère ne doit pas
    changer de constitution au milieu d'une chaîne, et le registre doit
    dire vrai sur ce qui a gouverné.

    Le repli n'existe donc QU'au pas 1. Aux pas ≥ 2 une lecture en échec
    lève un retryable : faire rejouer la chaîne est moins grave que la
    faire finir sous un autre texte que celui qu'elle a commencé.

    ⚠ Une chaîne née en repli est épinglée à la version SOURCE, qui
    n'existera jamais dans ``versions/``. Une lecture cléée y échouerait
    POUR TOUJOURS — retryable, redélivrance, retryable — jusqu'à
    l'épuisement des reprises. D'où le court-circuit : la version source se
    résout sans toucher Firestore.
    """
    epingle = turn.get("charter_version")
    if epingle is not None:
        if charter_model.is_source_version(epingle):
            return charter.source_charter(), charter.SOURCE_CHARTER_VERSION, ""
        doc = charter_model.get_version(int(epingle))
        if doc is None:
            raise vertex.ChatVertexRetryable("charter_unreadable")
        resolue = charter.charter_from_head(
            {**doc, "current_version": doc.get("version")}
        )
        return resolue, int(epingle), ""

    head, statut = charter_model.get_head()
    if statut == "ok" and head is not None:
        return (
            charter.charter_from_head(head),
            int(head.get("current_version") or 0),
            "firestore",
        )
    # « absent » est l'état NORMAL d'un déploiement neuf — silencieux.
    # « erreur » veut dire qu'une charte existe et n'a pas été appliquée.
    return (
        charter.source_charter(),
        charter.SOURCE_CHARTER_VERSION,
        "amorcage" if statut == "absent" else "repli",
    )


def _build_tools(*, unattended: bool = False) -> list[dict]:
    tools = registry.anthropic_tools(unattended=unattended)
    if tools:
        # The entry dicts are fresh per call (only input_schema is shared by
        # identity), so the trailing cache breakpoint never mutates TOOLS.
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


# ── Tool phase ──────────────────────────────────────────────────────────────


def _tool_use_blocks(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if b.get("type") == "tool_use"]


def _run_tools(
    tool_uses: list[dict],
    conv: dict,
    turn: dict,
    *,
    refused_ids: frozenset = frozenset(),
    skill_pairs: Optional[list] = None,
    charter_version: Optional[int] = None,
) -> list[dict]:
    unattended = turn.get("addendum") == "unattended"
    seed = (
        f"{conv.get('scheduled_task_id', '')}|{conv['id']}|"
        f"{turn['id']}|{int(turn.get('step') or 0)}"
    )
    # THIS turn's (skill_id, version) pairs: the stamped turn doc when it
    # has them (step 2+, resume-from-authorization — the pause commit
    # stamped them), else the in-memory pairs the caller resolved for this
    # very assembly (step 1, whose commit stamps the same list). The only
    # route get_skill_file may resolve through, and what draft provenance
    # records — never a re-read head.
    effective_pairs = turn.get("skill_versions") or skill_pairs or []
    # Same rule for the charter, which had no in-memory fallback: at step 1
    # the doc still reads None (the stamp lands on the commit that FOLLOWS
    # the tools), so the most common case of all — a save_draft in the very
    # first tool batch — recorded provenance without the charter that
    # governed it. `is not None`, never `or`: version 0 does not exist, and
    # the `or` habit is precisely what made the stamp guard below unable to
    # guard a conversation with no skills.
    stamped_charter = turn.get("charter_version")
    charte_effective = (
        stamped_charter if stamped_charter is not None else charter_version
    )
    # ⚠ DEUX listes, et la distinction est porteuse. La provenance
    # enregistre les compétences PURES ; la résolution y ajoute la
    # charte sous son identifiant réservé, pour que get_skill_file
    # trouve sa version épinglée par le MÊME chemin. Une seule variable
    # ferait apparaître une fausse compétence nommée « charte » dans
    # le skill_versions de chaque brouillon.
    resolution_pairs = list(effective_pairs)
    if charte_effective is not None:
        resolution_pairs.append(
            {"skill_id": charter.CHARTER_FILE_ID, "version": int(charte_effective)}
        )
    provenance_extra = {
        "model": conv.get("model", ""),
        "skill_versions": effective_pairs,
        "charter_version": charte_effective,

    }
    # ONE mail budget for the whole batch, not one per call. The tool phase
    # runs in the SAME gunicorn request as the Vertex call it follows
    # (chat.yaml: --timeout 570) and this loop has no length check, so
    # per-call ceilings do not compose into a per-request bound: three long
    # filings would SIGKILL the worker mid-batch, which is the one failure an
    # at-least-once chain cannot recover from cleanly.
    mail_context = {
        "owner_uid": str(conv.get("owner_uid", "")),
        "conversation_dossier_id": str(conv.get("dossier_id", "")),
        "unattended": unattended,
        "idempotency_seed": seed,
        # A LIST so the count survives execute_tool's per-call copy of this
        # dict: {**ctx} copies references, so every call in the batch shares
        # this one object. The batch loop below has no length check of its
        # own, which is exactly what the cap exists for.
        "batch_calls": [0],
    }
    mail_budget = graph_messagerie.start_budget()
    results: list[dict] = []
    attachments: list[dict] = []
    try:
        return _run_tool_batch(
            tool_uses, conv, turn, refused_ids, resolution_pairs,
            provenance_extra, unattended, seed, mail_context, results,
            attachments,
        )
    finally:
        graph_messagerie.reset_budget(mail_budget)


def _run_tool_batch(
    tool_uses, conv, turn, refused_ids, resolution_pairs, provenance_extra,
    unattended, seed, mail_context, results, attachments,
):
    for block in tool_uses:
        tool_use_id = str(block.get("id", ""))
        name = str(block.get("name", ""))
        if tool_use_id in refused_ids:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [
                        {"type": "text", "text": _REFUSED_BY_LAWYER_FR}
                    ],
                    "is_error": True,
                }
            )
            log_chat_event(
                "chat_authorization",
                "refused",
                conversation_id=conv["id"],
                turn_id=turn["id"],
                tool=name,
                decision="refuse",
            )
            continue
        with span("chat.tool", tool=name):
            execution = executors.execute_tool(
                name,
                block.get("input") or {},
                conversation_id=conv["id"],
                turn_id=turn["id"],
                step=int(turn.get("step") or 0),
                unattended=unattended,
                idempotency_seed=seed,
                tool_use_id=tool_use_id,
                provenance_extra=provenance_extra,
                skill_pairs=resolution_pairs,
                mail_context=mail_context,
            )
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": [{"type": "text", "text": execution.content}],
                "is_error": execution.is_error,
            }
        )
        attachments.extend(_native_pdf_fallback(block, execution, conv, turn))
    # Attachments FOLLOW the tool_results inside the same user message —
    # tool_result blocks must come first in a user turn.
    return results + attachments


def _native_pdf_fallback(
    tool_use: dict, execution, conv: dict, turn: dict
) -> list[dict]:
    """Decision D2's second half: a fully scanned PDF window comes back
    with every returned page textless — attach the document itself as a
    native base64 block so the model reads it visually. Narrow by design:
    PDFs only, bounded size and page count, never on an error result."""
    if tool_use.get("name") != "get_document_text" or execution.is_error:
        return []
    try:
        # raw_content, never content: the provenance envelope wraps the
        # payload for the model, and parsing the wrapped form would fail
        # into a silent [] — the scanned exhibit would just stop working.
        payload = json.loads(getattr(execution, "raw_content", "") or execution.content)
    except ValueError:
        return []
    if not (
        payload.get("found")
        and payload.get("readable")
        and payload.get("pagination_unit") == "page"
        and payload.get("pages")
        and all(not p.get("has_text") for p in payload["pages"])
    ):
        return []
    if int(payload.get("page_count") or 0) > _NATIVE_PDF_MAX_PAGES:
        return [
            {
                "type": "text",
                "text": (
                    "(Pièce entièrement numérisée, trop longue pour la "
                    "lecture native — consultez-la dans l'application.)"
                ),
            }
        ]
    document_id = str((tool_use.get("input") or {}).get("document_id", ""))
    data, reason = document_model.get_document_bytes(
        document_id, max_bytes=_NATIVE_PDF_MAX_BYTES
    )
    if data is None:
        return [
            {
                "type": "text",
                "text": (
                    "(Pièce entièrement numérisée ; la lecture native n'est "
                    f"pas possible ici — {reason}.)"
                ),
            }
        ]
    return [
        {
            "type": "text",
            "text": (
                "(Pièce jointe en PDF natif — les pages retournées n'ont "
                "aucune couche texte ; lisez le document ci-joint.)"
            ),
        },
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(data).decode("ascii"),
            },
        },
    ]


# ── The task driver ─────────────────────────────────────────────────────────


def _avis_echec(conversation_id: str) -> None:
    """Prévenir l'avocat qu'une exécution planifiée est morte.

    Best-effort par contrat, comme la livraison du rapport : un problème de
    courriel ne doit jamais faire échouer — ni rejouer — un tour déjà
    terminalisé. Le marqueur d'au-plus-une-fois est partagé avec le
    rapport, donc jamais les deux.
    """
    try:
        from chat import planification

        planification.livrer_echec(
            conv_model.get_conversation(conversation_id)
        )
    except Exception:
        log_unexpected(
            "chat failure notice failed", conversation_id=conversation_id
        )


def process_task(payload: dict, retry_count: int) -> str:
    """Drive one Cloud Tasks delivery. Returns a machine-stable outcome
    (for the route's logging); raises :class:`vertex.ChatVertexRetryable`
    when the delivery should be retried."""
    conversation_id = str(payload.get("conversation_id") or "")
    turn_id = str(payload.get("turn_id") or "")
    step_token = str(payload.get("step_token") or "")
    if not (conversation_id and turn_id and step_token):
        return "malformed"

    if retry_count >= Config.CHAT_TASK_RETRY_TERMINAL:
        conv_model.fail_turn(
            conversation_id, turn_id, reason="retry_exhausted"
        )
        log_chat_event(
            "chat_turn_failed",
            "failure",
            conversation_id=conversation_id,
            turn_id=turn_id,
            reason="retry_exhausted",
            retry_count=retry_count,
        )
        # L'avis d'échec emprunte le chemin du rapport : livrer_rapport ne
        # s'atteint QUE sur la branche de succès terminal, si bien qu'une
        # exécution planifiée morte ici ne disait rien à personne. Une
        # absence de courriel un mardi matin se confond avec « rien à
        # signaler ».
        _avis_echec(conversation_id)
        return "terminalized"

    status, turn, repair_token = conv_model.claim_step(
        conversation_id, turn_id, step_token
    )
    if status == "skip":
        log_chat_event(
            "chat_duplicate_delivery",
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
        return "skip"
    if status == "repair":
        _enqueue(conversation_id, turn_id, repair_token)
        log_chat_event(
            "chat_duplicate_delivery",
            conversation_id=conversation_id,
            turn_id=turn_id,
            repaired=True,
        )
        return "repair"

    conv = conv_model.get_conversation(conversation_id)
    if conv is None:
        # get_conversation swallows read errors into None, so a transient
        # outage is indistinguishable from a missing doc here. Retry: the
        # transient heals; the truly-missing terminalizes at the retry
        # ceiling with `retry_exhausted`.
        raise vertex.ChatVertexRetryable("conversation_unreadable")

    try:
        with span(
            "chat.turn",
            conversation_id=conversation_id,
            turn_id=turn_id,
            step=int(turn.get("step") or 0),
            model=conv.get("model", ""),
            scheduled=turn.get("addendum") == "unattended",
        ):
            return _advance(conv, turn, step_token)
    except vertex.ChatVertexRetryable:
        raise
    except _StorageRefCorrupt:
        conv_model.fail_turn(
            conversation_id, turn_id, reason="storage_ref_corrupt"
        )
        log_chat_event(
            "chat_turn_failed",
            "failure",
            conversation_id=conversation_id,
            turn_id=turn_id,
            reason="storage_ref_corrupt",
        )
        return "failed"
    except vertex.ChatVertexFatal as exc:
        conv_model.commit_step(
            conversation_id,
            turn_id,
            step_token,
            next_state="failed",
            error={
                "code": exc.reason,
                "http_status": exc.status,
                "excerpt": exc.excerpt,  # turn doc only — never logged
            },
        )
        log_chat_event(
            "chat_turn_failed",
            "failure",
            conversation_id=conversation_id,
            turn_id=turn_id,
            reason=exc.reason,
        )
        _avis_echec(conversation_id)
        return "failed"
    except Exception:
        # A deterministic code fault retried five times would re-pay up to
        # five model calls to reproduce itself — terminalize instead
        # (loud; recovery is a new user turn, SPEC §2.2).
        log_unexpected(
            "chat turn engine failed",
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
        conv_model.fail_turn(
            conversation_id, turn_id, reason="internal_error"
        )
        log_chat_event(
            "chat_turn_failed",
            "failure",
            conversation_id=conversation_id,
            turn_id=turn_id,
            reason="internal_error",
        )
        return "failed"


def _advance(conv: dict, turn: dict, step_token: str) -> str:
    conversation_id = conv["id"]
    turn_id = turn["id"]
    step = int(turn.get("step") or 0)

    # Pre-call ceiling: a crash loop must never buy calls past the cap.
    if step >= Config.CHAT_CHAIN_MAX_CALLS:
        conv_model.commit_step(
            conversation_id,
            turn_id,
            step_token,
            next_state="failed",
            error={"code": "chain_ceiling"},
        )
        log_chat_event(
            "chat_turn_failed",
            "failure",
            conversation_id=conversation_id,
            turn_id=turn_id,
            reason="chain_ceiling",
            model_calls=step,
        )
        return "failed"

    # Assembly — skills resolved at THIS turn's head (FLAG 4), charter
    # versioned, stable order end to end (the cache prefix depends on it).
    #
    # ⚠ ORDER IS LOAD-BEARING: this sits ABOVE the pending-tool block on
    # purpose. The pending block RUNS APPROVED TOOL CALLS — an interactive
    # turn injects no idempotency_key (executors.py, gated on `unattended`)
    # — so a raise after them has Cloud Tasks redeliver and replay every
    # write: duplicate notes, duplicate drafts. Nothing below may move
    # above this line.
    #
    # ⚠⚠ THIS ORDERING IS NOT SUFFICIENT, and the comment here claimed it
    # was until 2026-08-31: it said resolution was « the only part of
    # _advance that may raise a RETRYABLE ». That is false, and always
    # was. `vertex.call_model` — below, and the whole point of the
    # function — raises ChatVertexRetryable on 429, on 5xx, on a timeout,
    # and since 2026-08-31 on an empty response. Reproduced on the real
    # engine: approve a batch, have the next model call raise, and the
    # redelivery re-executes the whole batch (claim_step returns
    # « proceed » WITHOUT consuming the token, by design). The exposure is
    # narrow — interactive turns only, since a scheduled run auto-refuses
    # every gated tool — but it is real: a replayed `revise_draft` appends
    # a second version and moves the head twice, and nothing here can
    # delete either.
    #
    # The fix is to commit the pending tool_results BEFORE the model call
    # (a `running` commit rotating the token, so a redelivery finds
    # tool_results != None and skips the block), or to inject the
    # deterministic idempotency key on the resume path rather than only
    # when `unattended` — the seed is already stable. Deliberately left
    # for its own lot: it restructures the commit sequence of the most
    # delicate transaction in the system, and it long predates the change
    # that documented it.
    heads, skill_pairs = _resolve_skills(conv, turn)
    scheduled = turn.get("addendum") == "unattended"
    charte, charter_version, charter_source = _resolve_charter(turn)
    if charter_source == "repli":
        log_chat_event(
            "chat_charter_repli",
            "failure",
            conversation_id=conversation_id,
            turn_id=turn_id,
            step=step + 1,
        )
    system = charter.system_blocks(
        heads, scheduled=scheduled, charter=charte, conv=conv
    )
    tools = _build_tools(unattended=scheduled)

    # Pending tool work from an authorization decision (§4.6.3): the last
    # segment stopped on tool_use, no results yet, decision recorded.
    pending_results: Optional[list[dict]] = None
    segments = turn.get("segments") or []
    if segments:
        last = segments[-1]
        if (
            last.get("stop_reason") == "tool_use"
            and last.get("tool_results") is None
        ):
            decision = (turn.get("authorization") or {}).get("decision") or {}
            refused = frozenset(decision.get("refused") or [])
            tool_uses = _tool_use_blocks(_rehydrate_all(last.get("blocks")))
            pending_results = _store_blocks(
                _run_tools(
                    tool_uses,
                    conv,
                    turn,
                    refused_ids=refused,
                    charter_version=charter_version,
                ),
                conv,
                turn,
            )

    turns = conv_model.list_turns(conversation_id)
    if pending_results is not None and turns:
        # The just-computed results are not committed yet — splice them
        # into the assembly copy of the current turn.
        for candidate in turns:
            if candidate.get("id") == turn_id and candidate.get("segments"):
                candidate["segments"][-1] = {
                    **candidate["segments"][-1],
                    "tool_results": pending_results,
                }
    messages = _assemble_messages(turns, turn_id)

    started = time.monotonic()
    response = vertex.call_model(
        conv.get("model", ""), system=system, messages=messages, tools=tools
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    usage = response.get("usage") or {}
    stop_reason = str(response.get("stop_reason") or "")
    log_chat_event(
        "chat_model_call",
        conversation_id=conversation_id,
        turn_id=turn_id,
        model=conv.get("model", ""),
        step=step + 1,
        duration_ms=duration_ms,
        stop_reason=stop_reason,
        # Le motif du fournisseur, tel quel, à côté du motif traduit : deux
        # motifs Gemini distincts se traduisent en « end_turn », et le
        # journal ne permettait pas de les distinguer après coup.
        raw_stop_reason=str(response.get("raw_stop_reason") or ""),
        blocks=len(response.get("content") or []),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_creation_input_tokens=int(
            usage.get("cache_creation_input_tokens") or 0
        ),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        web_search_requests=int(
            (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
        ),
    )

    # web_search is the one channel whose payload leaves the tenant before any
    # code here sees it: a `server_tool_use` block is already a RESULT, never
    # routed through executors.py. Until now it had no log line at all — the
    # queries were recorded on the turn document and nowhere anyone looks.
    #
    # The query TEXT is deliberately not emitted. It is model-composed and can
    # carry a client name straight out of a privileged thread, and the
    # redaction filter does not scrub names. Length plus a short digest give a
    # burst an identity and let two identical searches be correlated; the text
    # itself stays on the turn document, where a forensic read can reach it.
    for _block in list(response.get("content") or []):
        if _block.get("type") != "server_tool_use":
            continue
        _query = str((_block.get("input") or {}).get("query") or "")
        log_chat_event(
            "chat_web_search",
            conversation_id=conversation_id,
            turn_id=turn_id,
            step=step + 1,
            tool=str(_block.get("name") or "web_search"),
            query_chars=len(_query),
            query_sha8=hashlib.sha256(_query.encode("utf-8")).hexdigest()[:8],
        )

    blocks = _store_blocks(list(response.get("content") or []), conv, turn)
    segment: dict[str, Any] = {
        "step": step + 1,
        "model": conv.get("model", ""),
        "blocks": blocks,
        "stop_reason": stop_reason,
        "usage": usage,
        "pricing": {
            "version": Config.CHAT_PRICING.get("version", ""),
            "usd_micros": vertex.segment_cost_usd_micros(
                usage, conv.get("model", "")
            ),
        },
        "tool_results": None,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    # Stamped ONCE per turn, at the first commit. The guard reads
    # `charter_version` — which `prepare_turn_pair` initialises to None —
    # and NOT `skill_versions`: a conversation with no skills selected has
    # `[]`, which is falsy, so the old guard let the stamp be rewritten at
    # every step of the chain. Harmless while the charter was a constant;
    # a silent mid-chain move of the registre's provenance the moment it
    # stops being one.
    stamps: Optional[dict] = None
    if turn.get("charter_version") is None:
        stamps = {
            "skill_versions": skill_pairs,
            "charter_version": charter_version,
            "charter_source": charter_source,
        }

    common = dict(
        segment=segment,
        last_segment_tool_results=pending_results,
        stamps=stamps,
    )

    if stop_reason == "tool_use":
        raw_tool_uses = _tool_use_blocks(list(response.get("content") or []))
        gated = [
            b for b in raw_tool_uses if registry.is_gated(str(b.get("name", "")))
        ]
        if gated and not scheduled:
            authorization = {
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "calls": [
                    {
                        "tool_use_id": str(b.get("id", "")),
                        "name": str(b.get("name", "")),
                        "gated": registry.is_gated(str(b.get("name", ""))),
                        "input_preview": _serialize_block(
                            b.get("input") or {}
                        )[:400],
                    }
                    for b in raw_tool_uses
                ],
                "decision": None,
            }
            status, _tok = conv_model.commit_step(
                conversation_id,
                turn_id,
                step_token,
                next_state="awaiting_authorization",
                authorization=authorization,
                **common,
            )
            if status == "lost_race":
                return _lost_race(conversation_id, turn_id)
            log_chat_event(
                "chat_authorization",
                conversation_id=conversation_id,
                turn_id=turn_id,
                decision="demandee",
                calls=len(raw_tool_uses),
            )
            return "paused"

        # Execute the whole batch now (the gated ones auto-refuse in
        # unattended context inside the executor). skill_pairs: at step 1
        # the turn doc is not yet stamped — the in-memory pairs of THIS
        # assembly are the truth (the same list the commit below stamps).
        segment["tool_results"] = _store_blocks(
            _run_tools(
                raw_tool_uses,
                conv,
                turn,
                skill_pairs=skill_pairs,
                charter_version=charter_version,
            ),
            conv,
            turn,
        )
        if step + 1 >= Config.CHAT_CHAIN_MAX_CALLS:
            status, _tok = conv_model.commit_step(
                conversation_id,
                turn_id,
                step_token,
                next_state="failed",
                error={"code": "chain_ceiling"},
                **common,
            )
            if status == "lost_race":
                return _lost_race(conversation_id, turn_id)
            log_chat_event(
                "chat_turn_failed",
                "failure",
                conversation_id=conversation_id,
                turn_id=turn_id,
                reason="chain_ceiling",
                model_calls=step + 1,
            )
            return "failed"
        return _commit_and_continue(conversation_id, turn_id, step_token, common)

    if stop_reason == "pause_turn":
        # Commit the paused assistant message verbatim and continue — on
        # the next call it is replayed unchanged as the last message
        # (falls out of the generic assembly rule; the review's finding).
        return _commit_and_continue(conversation_id, turn_id, step_token, common)

    # Terminal stops: end_turn / stop_sequence / max_tokens (the last is
    # surfaced as `truncated` — honest, still useful).
    status, _tok = conv_model.commit_step(
        conversation_id,
        turn_id,
        step_token,
        next_state="final",
        # « unknown » rejoint « max_tokens » : un motif d'arrêt que la table
        # de traduction ne connaît pas est un arrêt ANORMAL, et le tour ne
        # doit pas se présenter comme complet. Sans cela, une réponse
        # partielle sous OTHER / LANGUAGE / TOO_MANY_TOOL_CALLS se livrait
        # comme un rapport entier.
        truncated=(stop_reason in ("max_tokens", "unknown")),
        **common,
    )
    if status == "lost_race":
        return _lost_race(conversation_id, turn_id)
    all_segments = list(turn.get("segments") or []) + [segment]
    totals = conv_model.sum_segments(all_segments)
    log_chat_event(
        "chat_turn_finalized",
        conversation_id=conversation_id,
        turn_id=turn_id,
        model_calls=totals["model_calls"],
        total_input_tokens=totals["input_tokens"],
        total_output_tokens=totals["output_tokens"],
        usd_micros=totals["usd_micros"],
    )
    # §12.4 — deliver_email of a scheduled run. AFTER the committed
    # finalize, best-effort by contract: livrer_rapport owns its at-most-
    # once marker and swallows its own failures; a delivery problem must
    # never fail (or retry) a committed turn.
    if conv.get("origin") == "planifiee":
        try:
            from chat import planification

            planification.livrer_rapport(conv)
        except Exception:
            log_unexpected(
                "chat report delivery failed",
                conversation_id=conversation_id,
            )
    return "final"


def _commit_and_continue(
    conversation_id: str, turn_id: str, step_token: str, common: dict
) -> str:
    status, new_token = conv_model.commit_step(
        conversation_id,
        turn_id,
        step_token,
        next_state="running",
        **common,
    )
    if status == "lost_race":
        return _lost_race(conversation_id, turn_id)
    _enqueue(conversation_id, turn_id, new_token)
    return "continue"


def _enqueue(conversation_id: str, turn_id: str, token: Optional[str]) -> None:
    """Enqueue the continuation; failure raises RETRYABLE so the same task
    redelivers and lands in the claim's REPAIR branch (the token is already
    rotated with ``enqueued: False``)."""
    if not token:
        return
    try:
        taches.enfiler_tour(conversation_id, turn_id, token)
    except Exception as exc:
        log_chat_event(
            "chat_enqueue_failed",
            "failure",
            conversation_id=conversation_id,
            turn_id=turn_id,
            reason=type(exc).__name__,
        )
        raise vertex.ChatVertexRetryable("enqueue_failed") from exc
    conv_model.mark_enqueued(conversation_id, turn_id, token)


def _lost_race(conversation_id: str, turn_id: str) -> str:
    # A strictly concurrent duplicate double-paid one model call; the
    # loser's result is discarded. ERROR by doctrine — it must be SEEN
    # (bounded by the queue's max-concurrent-dispatches=2).
    log_chat_event(
        "chat_duplicate_delivery",
        "failure",
        conversation_id=conversation_id,
        turn_id=turn_id,
        reason="doublon_vertex",
    )
    return "lost_race"
