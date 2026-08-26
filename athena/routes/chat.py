"""Routes navigateur du clavardage (Phase N) — service default.

Tout est derrière la session Firebase + MFA (``@login_required``), CSRF
standard, français. POST + redirection (PRG) avec ``?erreur=``/``?message=``
— la discipline « un refus voyage en 2xx » (htmx n'échange que les 2xx),
et le fragment de tour en attente est LE premier polling du dépôt :
``hx-trigger="every 2s"`` arrêté par **HTTP 286** (support natif htmx
2.0.4, vérifié dans le fichier vendorisé).

AUCUNE route de suppression n'existe dans ce blueprint, par conception
(SPEC §1) — épinglé par le balayage statique de tests/test_chat_ui.py.
"""

from __future__ import annotations

import io as _io
import json
from datetime import datetime

from markupsafe import escape

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from auth import login_required
from config import Config
from models import chat_conversation as conv_model
from models import chat_draft as draft_model
from models import chat_skill as skill_model
from models import chat_scheduled_task as task_model
from models.dossier import get_dossier, list_dossiers
from utils.logging_setup import log_chat_event, log_unexpected

chat_bp = Blueprint("chat", __name__, url_prefix="/chat")

# Poll cadence of the pending-turn fragment; 286 terminates it (htmx-native).
POLL_INTERVAL = "every 2s"
HTTP_STOP_POLLING = 286

_ERREURS = {
    "en_cours": (
        "Un tour est déjà en cours dans cette conversation — attendez la "
        "fin de la réponse avant d'envoyer un nouveau message."
    ),
    "message_vide": "Le message est vide.",
    "enfilage": (
        "Le message est enregistré mais le traitement n'a pas pu démarrer. "
        "Utilisez « Relancer » sur le tour en attente."
    ),
    "conversation": "Conversation introuvable.",
    "dossier": "Dossier introuvable. Choisissez-le depuis la recherche.",
    "autorisation": "Cette demande d'autorisation n'est plus en attente.",
    "aucun_gabarit": (
        "Aucun gabarit d'impression de note n'est configuré. Téléversez-en "
        "un dans « Gabarits » avec le type « Note (impression) »."
    ),
    "gabarit": "La génération du document Word a échoué.",
    "brouillon": "Brouillon introuvable.",
}


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _models_fr() -> list[tuple[str, str]]:
    return [
        (key, cfg.get("label_fr", key))
        for key, cfg in Config.CHAT_MODELS.items()
    ]


def _base_context() -> dict:
    return {
        "models_fr": _models_fr(),
        "default_model": Config.CHAT_DEFAULT_MODEL,
        "erreur": _ERREURS.get(request.args.get("erreur", "")),
        "message": request.args.get("message", ""),
    }


def _usd_display(usd_micros: int) -> str:
    return f"{usd_micros / 1_000_000:.2f} $ US"


def _turn_view(turn: dict) -> dict:
    """The template-facing projection of a turn — flags computed here, so
    templates never reach into segments themselves."""
    view = dict(turn)
    if turn.get("role") != "assistant":
        return view
    segments = turn.get("segments") or []
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tools: list[dict] = []
    for segment in segments:
        for block in segment.get("blocks") or []:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif kind == "storage_ref":
                if block.get("original_type") == "thinking":
                    thinking_parts.append(block.get("preview", "") + " […]")
                elif block.get("original_type") == "text":
                    text_parts.append(block.get("preview", "") + " […]")
            elif kind == "tool_use":
                tools.append(
                    {
                        "name": block.get("name", ""),
                        "args_apercu": str(block.get("input", ""))[:400],
                        "resultat_apercu": "",
                        "is_error": False,
                    }
                )
            elif kind == "server_tool_use":
                tools.append(
                    {
                        "name": str(block.get("name", "web_search")),
                        "args_apercu": str(block.get("input", ""))[:400],
                        "resultat_apercu": "",
                        "is_error": False,
                    }
                )
        for result in segment.get("tool_results") or []:
            if result.get("type") != "tool_result":
                continue
            excerpt = ""
            for piece in result.get("content") or []:
                if piece.get("type") == "text":
                    excerpt = piece.get("text", "")[:400]
                    break
            matching = [
                t for t in tools if not t["resultat_apercu"]
            ]
            if matching:
                matching[0]["resultat_apercu"] = excerpt
                matching[0]["is_error"] = bool(result.get("is_error"))
    usage = conv_model.sum_segments(segments)
    view.update(
        {
            "texte": "\n\n".join(p for p in text_parts if p),
            "reflexion": "\n\n".join(p for p in thinking_parts if p),
            "outils": tools,
            "usage": usage,
            "usd_display": _usd_display(usage["usd_micros"]),
        }
    )
    return view


def _phase_fr(turn: dict) -> str:
    if turn.get("state") == "pending":
        return "réflexion…"
    segments = turn.get("segments") or []
    if not segments:
        return "réflexion…"
    last = segments[-1]
    if last.get("stop_reason") == "tool_use":
        names = [
            b.get("name", "")
            for b in last.get("blocks") or []
            if b.get("type") == "tool_use"
        ]
        if names:
            return f"outil : {', '.join(names[:3])}"
    if last.get("stop_reason") == "pause_turn":
        return "recherche…"
    return "rédaction…"


# ── Recherche de dossier (le clone maison) ──────────────────────────────────


@chat_bp.route("/dossier-search")
@login_required
def dossier_search() -> str:
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return (
            '<div class="px-3 py-2 text-sm text-gray-500">'
            "Tapez au moins 2 caractères…</div>"
        )
    dossiers = list_dossiers(search=q)[:10]
    if not dossiers:
        return (
            '<div class="px-3 py-2 text-sm text-gray-500">'
            "Aucun dossier trouvé</div>"
        )
    parts = ['<ul class="divide-y divide-gray-100">']
    for d in dossiers:
        parts.append(
            f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm" '
            f'data-id="{escape(d["id"])}" '
            f'data-label="{escape(d.get("file_number", ""))} — '
            f'{escape(d.get("title", ""))}">'
            f'<span class="font-medium">{escape(d.get("file_number", ""))}</span> '
            f'<span class="text-gray-500">{escape(d.get("title", ""))}</span></li>'
        )
    parts.append("</ul>")
    return "".join(parts)


# ── Conversations ───────────────────────────────────────────────────────────


@chat_bp.route("/")
@login_required
def chat_index() -> str:
    conversations = conv_model.list_conversations(limit=200)
    floating = [c for c in conversations if not c.get("dossier_id")]
    groups: dict[str, dict] = {}
    for c in conversations:
        dossier_id = c.get("dossier_id") or ""
        if not dossier_id:
            continue
        group = groups.setdefault(
            dossier_id,
            {
                "dossier_id": dossier_id,
                "file_number": c.get("dossier_file_number", ""),
                "title": c.get("dossier_title", ""),
                "rows": [],
            },
        )
        group["rows"].append(c)
    ctx = _base_context()
    ctx.update(
        {
            "flottantes": floating,
            "groupes": list(groups.values()),
            "tronque": len(conversations) >= 200,
            "usd_display": _usd_display,
        }
    )
    return render_template("chat/list.html", **ctx)


@chat_bp.route("/nouvelle")
@login_required
def conversation_new() -> str:
    ctx = _base_context()
    ctx["skills"] = [
        s for s in skill_model.list_skills() if s.get("active")
    ]
    ctx["prefill_dossier_id"] = request.args.get("dossier_id", "")
    return render_template("chat/new.html", **ctx)


@chat_bp.route("/", methods=["POST"])
@login_required
def conversation_create() -> Response:
    f = request.form
    dossier_id = f.get("dossier_id", "").strip()
    data = {
        "title": f.get("title", "").strip(),
        "model": f.get("model", Config.CHAT_DEFAULT_MODEL),
        "skill_selection": f.getlist("skills"),
        "origin": "interactive",
        "owner_uid": session.get("user_id", ""),
        "dossier_id": dossier_id,
    }
    if dossier_id:
        dossier = get_dossier(dossier_id)
        if not dossier:
            return redirect(url_for("chat.conversation_new", erreur="dossier"))
        data["dossier_file_number"] = dossier.get("file_number", "")
        data["dossier_title"] = dossier.get("title", "")
    premier = f.get("premier_message", "").strip()
    if not data["title"]:
        # FLAG 9 — the title defaults to the first message, truncated.
        data["title"] = (premier[:80] or "Nouvelle conversation").strip()
    conversation, errors = conv_model.create_conversation(data)
    if errors:
        return redirect(url_for("chat.conversation_new", erreur="dossier"))
    log_chat_event(
        "chat_conversation_created",
        conversation_id=conversation["id"],
        dossier_id=dossier_id or None,
        model=data["model"],
        skill_count=len(data["skill_selection"]),
        source="ui",
    )
    if premier:
        return _demarrer_tour(conversation["id"], premier)
    return redirect(url_for("chat.conversation_detail", conversation_id=conversation["id"]))


@chat_bp.route("/<conversation_id>")
@login_required
def conversation_detail(conversation_id: str) -> Response | str:
    conv = conv_model.get_conversation(conversation_id)
    if conv is None:
        return redirect(url_for("chat.chat_index", erreur="conversation"))
    conv_model.mark_read(conversation_id)
    turns = conv_model.list_turns(conversation_id)
    ctx = _base_context()
    ctx.update(
        {
            "conv": conv,
            "turns": [_turn_view(t) for t in turns],
            "turns_tronques": len(turns) >= 500,
            "active_turn_id": conv.get("active_turn_id") or "",
            "phase_fr": _phase_fr,
            "poll_interval": POLL_INTERVAL,
            "skills": [
                s for s in skill_model.list_skills() if s.get("active")
            ],
            "cout_display": _usd_display(
                int((conv.get("cost_snapshot") or {}).get("usd_micros_total") or 0)
            ),
            "model_label": Config.CHAT_MODELS.get(
                conv.get("model", ""), {}
            ).get("label_fr", conv.get("model", "")),
        }
    )
    return render_template("chat/conversation.html", **ctx)


def _demarrer_tour(conversation_id: str, texte: str) -> Response:
    turn, errors = conv_model.start_turn(conversation_id, texte)
    if errors:
        code = "en_cours" if any("déjà en cours" in e for e in errors) else "message_vide"
        return redirect(
            url_for("chat.conversation_detail", conversation_id=conversation_id,
                    erreur=code)
        )
    log_chat_event(
        "chat_turn_started",
        conversation_id=conversation_id,
        turn_id=turn["id"],
        scheduled=False,
    )
    token = (turn.get("continuation") or {}).get("token", "")
    try:
        from chat import taches

        taches.enfiler_tour(conversation_id, turn["id"], token)
        conv_model.mark_enqueued(conversation_id, turn["id"], token)
    except Exception:
        log_chat_event(
            "chat_enqueue_failed",
            "failure",
            conversation_id=conversation_id,
            turn_id=turn["id"],
        )
        return redirect(
            url_for("chat.conversation_detail", conversation_id=conversation_id,
                    erreur="enfilage")
        )
    return redirect(
        url_for("chat.conversation_detail", conversation_id=conversation_id)
    )


@chat_bp.route("/<conversation_id>/message", methods=["POST"])
@login_required
def message_post(conversation_id: str) -> Response:
    conv = conv_model.get_conversation(conversation_id)
    if conv is None:
        return redirect(url_for("chat.chat_index", erreur="conversation"))
    return _demarrer_tour(conversation_id, request.form.get("message", ""))


@chat_bp.route("/<conversation_id>/tour/<turn_id>/relancer", methods=["POST"])
@login_required
def tour_relancer(conversation_id: str, turn_id: str) -> Response:
    """Re-enqueue a chain whose enqueue never happened (loud banner path).
    Safe against duplicates: an already-running chain's claim skips."""
    turn = conv_model.get_turn(conversation_id, turn_id)
    continuation = (turn or {}).get("continuation") or {}
    if (
        turn is not None
        and turn.get("state") in ("pending", "running")
        and continuation.get("token")
        and not continuation.get("enqueued")
    ):
        try:
            from chat import taches

            taches.enfiler_tour(conversation_id, turn_id, continuation["token"])
            conv_model.mark_enqueued(
                conversation_id, turn_id, continuation["token"]
            )
        except Exception:
            log_chat_event(
                "chat_enqueue_failed",
                "failure",
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            return redirect(
                url_for("chat.conversation_detail",
                        conversation_id=conversation_id, erreur="enfilage")
            )
    return redirect(
        url_for("chat.conversation_detail", conversation_id=conversation_id)
    )


@chat_bp.route("/<conversation_id>/tour/<turn_id>/fragment")
@login_required
def tour_fragment(conversation_id: str, turn_id: str):
    """The polled fragment. Terminal states (and the authorization pause)
    answer HTTP 286 — htmx stops polling AND swaps the final render."""
    turn = conv_model.get_turn(conversation_id, turn_id)
    if turn is None:
        return ("", HTTP_STOP_POLLING)
    state = turn.get("state", "")
    if state in ("final", "failed", "awaiting_authorization"):
        html = render_template(
            "chat/_turn.html",
            t=_turn_view(turn),
            conv={"id": conversation_id},
        )
        return (html, HTTP_STOP_POLLING)
    return render_template(
        "chat/_pending.html",
        conv={"id": conversation_id},
        t=turn,
        phase=_phase_fr(turn),
        poll_interval=POLL_INTERVAL,
    )


def _decision(conversation_id: str, turn_id: str, approve: bool) -> Response:
    turn = conv_model.get_turn(conversation_id, turn_id)
    calls = ((turn or {}).get("authorization") or {}).get("calls") or []
    ids = [c.get("tool_use_id", "") for c in calls]
    approved, refused = (ids, []) if approve else ([], ids)
    status, token = conv_model.decide_authorization(
        conversation_id, turn_id, approved=approved, refused=refused
    )
    if status != "ok":
        return redirect(
            url_for("chat.conversation_detail", conversation_id=conversation_id,
                    erreur="autorisation")
        )
    log_chat_event(
        "chat_authorization",
        "success" if approve else "refused",
        conversation_id=conversation_id,
        turn_id=turn_id,
        decision="approuve" if approve else "refuse",
    )
    try:
        from chat import taches

        taches.enfiler_tour(conversation_id, turn_id, token)
        conv_model.mark_enqueued(conversation_id, turn_id, token)
    except Exception:
        log_chat_event(
            "chat_enqueue_failed",
            "failure",
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
        return redirect(
            url_for("chat.conversation_detail", conversation_id=conversation_id,
                    erreur="enfilage")
        )
    return redirect(
        url_for("chat.conversation_detail", conversation_id=conversation_id)
    )


@chat_bp.route("/<conversation_id>/tour/<turn_id>/approuver", methods=["POST"])
@login_required
def tour_approuver(conversation_id: str, turn_id: str) -> Response:
    return _decision(conversation_id, turn_id, approve=True)


@chat_bp.route("/<conversation_id>/tour/<turn_id>/refuser", methods=["POST"])
@login_required
def tour_refuser(conversation_id: str, turn_id: str) -> Response:
    return _decision(conversation_id, turn_id, approve=False)


@chat_bp.route("/<conversation_id>/competences", methods=["POST"])
@login_required
def conversation_skills(conversation_id: str) -> Response:
    """Change the skill selection — effective at the NEXT turn (§5); the
    exact versions used are recorded on each turn regardless."""
    conv_model.set_skill_selection(
        conversation_id, request.form.getlist("skills")
    )
    return redirect(
        url_for("chat.conversation_detail", conversation_id=conversation_id)
    )


# ── Compétences (skills — §5) ───────────────────────────────────────────────


def _parse_files_json() -> tuple[list[dict], list[str]]:
    """The repeater's single hidden field (the budgets `lines_json`
    pattern): reparse server-side and coerce to strings — the MODEL stays
    the authority for every cap and duplicate rule."""
    raw = request.form.get("files_json") or "[]"
    try:
        parsed = json.loads(raw)
    except ValueError:
        return [], [
            "Le format des fichiers de référence est invalide — rechargez "
            "la page et réessayez."
        ]
    if not isinstance(parsed, list):
        return [], [
            "Le format des fichiers de référence est invalide — rechargez "
            "la page et réessayez."
        ]
    rows: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "name": str(entry.get("name", "")),
                "description": str(entry.get("description", "")),
                "content": str(entry.get("content", "")),
            }
        )
    return rows, []


@chat_bp.route("/competences")
@login_required
def skills_list() -> str:
    ctx = _base_context()
    ctx["skills"] = skill_model.list_skills()
    return render_template("chat/skills_list.html", **ctx)


@chat_bp.route("/competences/nouvelle")
@login_required
def skill_new() -> str:
    ctx = _base_context()
    ctx["skill"] = None
    ctx["files_seed"] = []
    return render_template("chat/skill_form.html", **ctx)


@chat_bp.route("/competences", methods=["POST"])
@login_required
def skill_create() -> Response:
    f = request.form
    files, parse_errors = _parse_files_json()
    if parse_errors:
        ctx = _base_context()
        ctx.update(
            {"skill": None, "erreurs": parse_errors, "form": f,
             "files_seed": files}
        )
        return render_template("chat/skill_form.html", **ctx)
    skill, errors = skill_model.create_skill(
        {
            "name": f.get("name", "").strip(),
            "description": f.get("description", "").strip(),
            "body": f.get("body", "").strip(),
            "files": files,
        }
    )
    if errors:
        ctx = _base_context()
        # files_seed = the submitted rows — the user's work survives the
        # re-render (the form re-seeds its repeater from them).
        ctx.update(
            {"skill": None, "erreurs": errors, "form": f, "files_seed": files}
        )
        return render_template("chat/skill_form.html", **ctx)
    log_chat_event(
        "chat_skill_saved",
        skill_id=skill["id"],
        version=1,
        active=True,
        files_count=len(skill.get("files") or []),
    )
    return redirect(url_for("chat.skill_detail", skill_id=skill["id"]))


@chat_bp.route("/competences/<skill_id>")
@login_required
def skill_detail(skill_id: str) -> Response | str:
    skill = skill_model.get_skill(skill_id)
    if skill is None:
        return redirect(url_for("chat.skills_list"))
    ctx = _base_context()
    ctx.update(
        {
            "skill": skill,
            "versions": skill_model.list_versions(skill_id),
            "version_affichee": request.args.get("version", ""),
        }
    )
    manifest = skill.get("files") or []
    version = request.args.get("version", "")
    if version.isdigit():
        vdoc = skill_model.get_version(skill_id, int(version))
        if vdoc:
            ctx["corps_affiche"] = vdoc.get("body", "")
            ctx["version_affichee"] = version
            manifest = vdoc.get("files") or []
    ctx["fichiers"] = skill_model.list_file_contents(skill_id, manifest)
    return render_template("chat/skill_detail.html", **ctx)


@chat_bp.route("/competences/<skill_id>/modifier")
@login_required
def skill_edit(skill_id: str) -> Response | str:
    skill = skill_model.get_skill(skill_id)
    if skill is None:
        return redirect(url_for("chat.skills_list"))
    ctx = _base_context()
    ctx["skill"] = skill
    ctx["files_seed"] = skill_model.list_file_contents(
        skill_id, skill.get("files") or []
    )
    return render_template("chat/skill_form.html", **ctx)


@chat_bp.route("/competences/<skill_id>", methods=["POST"])
@login_required
def skill_revise(skill_id: str) -> Response | str:
    f = request.form
    files, parse_errors = _parse_files_json()
    if parse_errors:
        ctx = _base_context()
        ctx.update(
            {"skill": skill_model.get_skill(skill_id), "erreurs": parse_errors,
             "form": f, "files_seed": files}
        )
        return render_template("chat/skill_form.html", **ctx)
    skill, errors = skill_model.revise_skill(
        skill_id,
        body=f.get("body", "").strip(),
        name=f.get("name", "").strip() or None,
        description=f.get("description", "").strip() or None,
        files=files,
    )
    if errors:
        ctx = _base_context()
        ctx.update(
            {"skill": skill_model.get_skill(skill_id), "erreurs": errors,
             "form": f, "files_seed": files}
        )
        return render_template("chat/skill_form.html", **ctx)
    log_chat_event(
        "chat_skill_saved",
        skill_id=skill_id,
        version=skill["current_version"],
        active=bool(skill.get("active")),
        files_count=len(skill.get("files") or []),
    )
    return redirect(url_for("chat.skill_detail", skill_id=skill_id))


@chat_bp.route("/competences/<skill_id>/activer", methods=["POST"])
@login_required
def skill_toggle(skill_id: str) -> Response:
    actif = request.form.get("actif") == "on"
    skill, _errors = skill_model.set_active(skill_id, actif)
    if skill:
        log_chat_event(
            "chat_skill_saved",
            skill_id=skill_id,
            version=int(skill.get("current_version") or 0),
            active=actif,
        )
    return redirect(url_for("chat.skill_detail", skill_id=skill_id))


# ── Tâches planifiées (§12) ─────────────────────────────────────────────────


def _task_form_data() -> dict:
    f = request.form
    data = {
        "name": f.get("name", "").strip(),
        "prompt": f.get("prompt", "").strip(),
        "model": f.get("model", Config.CHAT_DEFAULT_MODEL),
        "skill_selection": f.getlist("skills"),
        "recurrence": {
            "kind": f.get("recurrence", "quotidien"),
            "day": int(f.get("jour", "0") or 0),
        },
        "hour_local": int(f.get("hour_local", "7") or 7),
        "deliver_email": f.get("deliver_email") == "on",
        "dossier_id": f.get("dossier_id", "").strip(),
    }
    return data


def _enrich_task_dossier(data: dict) -> tuple[dict, list[str]]:
    dossier_id = data.get("dossier_id", "")
    if not dossier_id:
        data["dossier_file_number"] = ""
        data["dossier_title"] = ""
        return data, []
    dossier = get_dossier(dossier_id)
    if not dossier:
        return data, ["Dossier introuvable."]
    data["dossier_file_number"] = dossier.get("file_number", "")
    data["dossier_title"] = dossier.get("title", "")
    return data, []


@chat_bp.route("/taches-planifiees")
@login_required
def tasks_list() -> str:
    ctx = _base_context()
    ctx.update(
        {
            "tasks": task_model.list_tasks(),
            "recurrence_labels": task_model.RECURRENCE_LABELS,
            "weekday_labels": task_model.WEEKDAY_LABELS,
        }
    )
    return render_template("chat/tasks_list.html", **ctx)


@chat_bp.route("/taches-planifiees/nouvelle")
@login_required
def task_new() -> str:
    ctx = _base_context()
    ctx.update(
        {
            "task": None,
            "skills": [s for s in skill_model.list_skills() if s.get("active")],
            "recurrence_labels": task_model.RECURRENCE_LABELS,
            "weekday_labels": task_model.WEEKDAY_LABELS,
        }
    )
    return render_template("chat/task_form.html", **ctx)


@chat_bp.route("/taches-planifiees", methods=["POST"])
@login_required
def task_create() -> Response | str:
    data, errors = _enrich_task_dossier(_task_form_data())
    if not errors:
        task, errors = task_model.create_task(data)
    if errors:
        ctx = _base_context()
        ctx.update(
            {
                "task": None,
                "erreurs": errors,
                "form": request.form,
                "skills": [s for s in skill_model.list_skills() if s.get("active")],
                "recurrence_labels": task_model.RECURRENCE_LABELS,
                "weekday_labels": task_model.WEEKDAY_LABELS,
            }
        )
        return render_template("chat/task_form.html", **ctx)
    log_chat_event(
        "chat_task_saved",
        task_id=task["id"],
        active=True,
        recurrence=data["recurrence"]["kind"],
    )
    return redirect(url_for("chat.tasks_list"))


@chat_bp.route("/taches-planifiees/<task_id>/modifier")
@login_required
def task_edit(task_id: str) -> Response | str:
    task = task_model.get_task(task_id)
    if task is None:
        return redirect(url_for("chat.tasks_list"))
    ctx = _base_context()
    ctx.update(
        {
            "task": task,
            "skills": [s for s in skill_model.list_skills() if s.get("active")],
            "recurrence_labels": task_model.RECURRENCE_LABELS,
            "weekday_labels": task_model.WEEKDAY_LABELS,
        }
    )
    return render_template("chat/task_form.html", **ctx)


@chat_bp.route("/taches-planifiees/<task_id>", methods=["POST"])
@login_required
def task_update(task_id: str) -> Response | str:
    data, errors = _enrich_task_dossier(_task_form_data())
    if not errors:
        task, errors = task_model.update_task(task_id, data)
    if errors:
        ctx = _base_context()
        ctx.update(
            {
                "task": task_model.get_task(task_id),
                "erreurs": errors,
                "form": request.form,
                "skills": [s for s in skill_model.list_skills() if s.get("active")],
                "recurrence_labels": task_model.RECURRENCE_LABELS,
                "weekday_labels": task_model.WEEKDAY_LABELS,
            }
        )
        return render_template("chat/task_form.html", **ctx)
    log_chat_event(
        "chat_task_saved",
        task_id=task_id,
        active=bool(task.get("active")),
        recurrence=data["recurrence"]["kind"],
    )
    return redirect(url_for("chat.tasks_list"))


@chat_bp.route("/taches-planifiees/<task_id>/activer", methods=["POST"])
@login_required
def task_toggle(task_id: str) -> Response:
    actif = request.form.get("actif") == "on"
    task, _errors = task_model.set_active(task_id, actif)
    if task:
        log_chat_event(
            "chat_task_saved",
            task_id=task_id,
            active=actif,
            recurrence=(task.get("recurrence") or {}).get("kind", ""),
        )
    return redirect(url_for("chat.tasks_list"))


# ── Brouillons (§4.3/D4) ────────────────────────────────────────────────────


@chat_bp.route("/brouillons")
@login_required
def drafts_list() -> str:
    drafts = draft_model.list_drafts()
    floating = [d for d in drafts if not d.get("dossier_id")]
    groups: dict[str, dict] = {}
    for d in drafts:
        dossier_id = d.get("dossier_id") or ""
        if not dossier_id:
            continue
        group = groups.setdefault(
            dossier_id,
            {
                "dossier_id": dossier_id,
                "file_number": d.get("dossier_file_number", ""),
                "title": d.get("dossier_title", ""),
                "rows": [],
            },
        )
        group["rows"].append(d)
    ctx = _base_context()
    ctx.update({"flottants": floating, "groupes": list(groups.values())})
    return render_template("chat/drafts_list.html", **ctx)


@chat_bp.route("/brouillons/<draft_id>")
@login_required
def draft_detail(draft_id: str) -> Response | str:
    draft = draft_model.get_draft(draft_id)
    if draft is None:
        return redirect(url_for("chat.drafts_list", erreur="brouillon"))
    ctx = _base_context()
    corps = draft.get("content", "")
    version_affichee = int(draft.get("current_version") or 0)
    version = request.args.get("version", "")
    if version.isdigit() and int(version) != version_affichee:
        vdoc = draft_model.get_version(draft_id, int(version))
        if vdoc:
            corps = vdoc.get("content", "")
            version_affichee = int(version)
    ctx.update(
        {
            "draft": draft,
            "corps": corps,
            "version_affichee": version_affichee,
            "versions": draft_model.list_versions(draft_id),
        }
    )
    return render_template("chat/draft_detail.html", **ctx)


@chat_bp.route("/brouillons/<draft_id>/verser-word", methods=["POST"])
@login_required
def draft_verser_word(draft_id: str) -> Response:
    """« Verser en Word » — the H.3 pipeline on a draft: with a dossier the
    .docx lands in « Projets » (the generated-documents home); a floating
    draft downloads directly (nowhere to file it)."""
    from flask import send_file
    from werkzeug.utils import secure_filename

    from models.doc_template import (
        DOCX_MIME,
        get_note_template,
        get_template_bytes,
    )
    from tz import MTL
    from utils.cabinet import cabinet_dict
    from utils.docx_fill import DocxFillError, fill_docx
    from utils.logging_setup import log_template_event
    from utils.note_docx import assemble_note_print_values, build_note_context

    draft = draft_model.get_draft(draft_id)
    if draft is None:
        return redirect(url_for("chat.drafts_list", erreur="brouillon"))
    if _is_htmx():
        # An HTMX submit would swap raw .docx bytes into the page.
        return redirect(
            url_for("chat.draft_detail", draft_id=draft_id, erreur="gabarit")
        )
    template = get_note_template()
    if not template:
        return redirect(
            url_for("chat.draft_detail", draft_id=draft_id,
                    erreur="aucun_gabarit")
        )
    dossier_id = draft.get("dossier_id", "")
    dossier = get_dossier(dossier_id) if dossier_id else None
    today = datetime.now(MTL).date()
    note_like = {
        "title": draft.get("title", ""),
        "content": draft.get("content", ""),
        # Renders through the label fallback as the raw string « Brouillon ».
        "category": "Brouillon",
        "created_at": draft.get("created_at"),
        "updated_at": draft.get("updated_at"),
        "dossier_id": dossier_id,
        "dossier_file_number": draft.get("dossier_file_number", ""),
        "dossier_title": draft.get("dossier_title", ""),
    }
    try:
        ctx = build_note_context(
            note_like, dossier=dossier, firm=cabinet_dict(), today=today
        )
        values = assemble_note_print_values(template, ctx)
        docx_bytes = get_template_bytes(template["id"])
        if docx_bytes is None:
            raise DocxFillError("gabarit indisponible")
        filled = fill_docx(docx_bytes, values, rich_values=ctx.rich_values)
    except Exception:
        log_unexpected("draft verser-word failed", draft_id=draft_id)
        log_template_event(
            "generation_failed",
            template_id=template.get("id"),
            reason="fill_error",
        )
        return redirect(
            url_for("chat.draft_detail", draft_id=draft_id, erreur="gabarit")
        )

    reference = draft.get("dossier_file_number", "") or "Brouillon"
    display = " - ".join(
        p
        for p in (reference, today.isoformat(),
                  f"Projet {draft.get('title', '')[:60]}")
        if p
    )
    out_name = secure_filename(f"{display}.docx") or f"brouillon_{today}.docx"

    if dossier_id and dossier:
        from models.document import (
            GENERATED_FOLDER_NAME,
            projet_document_name,
            upload_document,
        )
        from models.folder import get_or_create_folder

        folder = get_or_create_folder(dossier_id, GENERATED_FOLDER_NAME)
        display_name = projet_document_name(
            draft.get("dossier_file_number", ""), draft.get("title", ""), today
        )
        document, errors = upload_document(
            dossier_id,
            draft.get("dossier_file_number", ""),
            _io.BytesIO(filled),
            out_name,
            len(filled),
            {
                "category": "autre",
                "folder_id": (folder or {}).get("id"),
                "display_name": display_name,
                "description": (
                    f"Versé depuis le brouillon "
                    f"v{draft.get('current_version', '')}"
                ),
                "tags": ["brouillon"],
            },
            session.get("user_id", ""),
        )
        if errors:
            return redirect(
                url_for("chat.draft_detail", draft_id=draft_id, erreur="gabarit")
            )
        log_chat_event(
            "chat_draft_exported",
            draft_id=draft_id,
            version=int(draft.get("current_version") or 0),
            document_id=(document or {}).get("id"),
            dossier_id=dossier_id,
        )
        return redirect(
            url_for("chat.draft_detail", draft_id=draft_id,
                    message="Versé dans « Projets » du dossier.")
        )

    log_chat_event(
        "chat_draft_exported",
        draft_id=draft_id,
        version=int(draft.get("current_version") or 0),
    )
    return send_file(
        _io.BytesIO(filled),
        mimetype=DOCX_MIME,
        as_attachment=True,
        download_name=out_name,
    )
