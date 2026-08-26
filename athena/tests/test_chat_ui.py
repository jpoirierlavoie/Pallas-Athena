"""routes/chat.py + templates/chat/* — the browser slice (Phase N).

Real-render smoke tests (the test_admin_integration web_rendu pattern),
the polling/286 contract (the repo's FIRST hx-trigger="every"), the nav
pins (both renders), the no-delete static sweep (SPEC §13.5), and the
closed recurrence vocabulary pin.
"""

import json as _json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.chat as rc

from flask import Flask  # noqa: E402

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = Path(_ATHENA) / "templates"


@pytest.fixture()
def web_rendu(monkeypatch):
    from markupsafe import Markup

    import bleach as _bleach
    import markdown as _markdown_lib

    from tz import to_mtl
    from utils.icons import ms as _ms
    from utils.markdown_docx import (
        ALLOWED_ATTRS,
        ALLOWED_TAGS,
        MD_EXTENSION_CONFIGS,
        MD_EXTENSIONS,
    )

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.jinja_env.filters["to_mtl"] = to_mtl

    def _render_markdown(text):
        html = _markdown_lib.markdown(
            text or "",
            extensions=MD_EXTENSIONS,
            extension_configs=MD_EXTENSION_CONFIGS,
        )
        return _bleach.clean(
            html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True
        )

    app.jinja_env.filters["markdown"] = _render_markdown

    def _jsattr(value):
        js = _json.dumps(str(value), ensure_ascii=False)
        return Markup(
            js.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
        )

    app.jinja_env.filters["jsattr"] = _jsattr
    app.register_blueprint(rc.chat_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


_NOW = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)


def _conv(**over):
    c = {
        "id": "c1", "title": "Analyse du contrat", "model": "claude-sonnet-5",
        "dossier_id": "", "dossier_file_number": "", "dossier_title": "",
        "skill_selection": [], "origin": "interactive", "unread": False,
        "active_turn_id": "", "turn_count": 2,
        "token_totals": {"input_tokens": 100, "output_tokens": 50,
                         "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": 0,
                         "web_search_requests": 0, "model_calls": 1},
        "cost_snapshot": {"usd_micros_total": 123456, "pricing_version": "x"},
        "created_at": _NOW, "updated_at": _NOW,
    }
    c.update(over)
    return c


def _final_turn(**over):
    t = {
        "id": "000002", "seq": 2, "role": "assistant", "state": "final",
        "step": 1, "step_token": "tok", "continuation": None,
        "segments": [{
            "step": 1, "model": "claude-sonnet-5",
            "blocks": [
                {"type": "thinking", "thinking": "Je réfléchis au dossier.",
                 "signature": "s"},
                {"type": "tool_use", "id": "t1", "name": "get_dossier",
                 "input": {"dossier_id": "d1"}},
                {"type": "text", "text": "## Analyse\n\nVoici ma **réponse**."},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "pricing": {"version": "x", "usd_micros": 123456},
            "tool_results": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": '{"found": true}'}],
                 "is_error": False},
            ],
        }],
        "authorization": None, "skill_versions": [], "charter_version": 1,
        "addendum": "", "error": None, "truncated": False,
        "created_at": _NOW, "updated_at": _NOW,
    }
    t.update(over)
    return t


def _user_turn():
    return {
        "id": "000001", "seq": 1, "role": "user",
        "content": [{"type": "text", "text": "Analyse ce contrat."}],
        "by": "juriste", "created_at": _NOW,
    }


# ── Rendered smoke ──────────────────────────────────────────────────────────

def test_list_renders_groups_flottantes_and_markers(web_rendu, monkeypatch):
    monkeypatch.setattr(
        rc.conv_model, "list_conversations",
        lambda limit=200: [
            _conv(unread=True, active_turn_id="000002"),
            _conv(id="c2", title="Sur dossier", dossier_id="d1",
                  dossier_file_number="2026-001", dossier_title="Tremblay"),
        ],
    )
    html = web_rendu.get("/chat/").get_data(as_text=True)
    assert "Flottantes" in html
    assert "2026-001 — Tremblay" in html
    assert "nouveau" in html        # the unread chip
    assert "en cours" in html       # the in-flight chip
    assert "Nouvelle conversation" in html


def test_conversation_view_renders_turns_thinking_tools_and_cost(web_rendu, monkeypatch):
    monkeypatch.setattr(rc.conv_model, "get_conversation", lambda i: _conv())
    monkeypatch.setattr(rc.conv_model, "mark_read", lambda i: None)
    monkeypatch.setattr(
        rc.conv_model, "list_turns",
        lambda i, limit=500: [_user_turn(), _final_turn()],
    )
    monkeypatch.setattr(rc.skill_model, "list_skills", lambda: [])
    html = web_rendu.get("/chat/c1").get_data(as_text=True)
    assert "Analyse ce contrat." in html
    assert "Réflexion" in html and "Je réfléchis au dossier." in html
    assert "outil : get_dossier" in html
    assert "<h2>Analyse</h2>" in html            # markdown rendered
    assert "note-content" in html
    assert "0.12 $ US" in html                   # the cost indicator
    assert "Envoyer" in html                     # the composer is live


def test_failed_turn_renders_the_loud_banner(web_rendu, monkeypatch):
    failed = _final_turn(state="failed", error={"code": "chain_ceiling"})
    monkeypatch.setattr(rc.conv_model, "get_conversation", lambda i: _conv())
    monkeypatch.setattr(rc.conv_model, "mark_read", lambda i: None)
    monkeypatch.setattr(
        rc.conv_model, "list_turns", lambda i, limit=500: [failed]
    )
    monkeypatch.setattr(rc.skill_model, "list_skills", lambda: [])
    html = web_rendu.get("/chat/c1").get_data(as_text=True)
    assert "Le tour a échoué" in html
    assert "chain_ceiling" in html


def test_authorization_card_renders_both_decisions(web_rendu, monkeypatch):
    waiting = _final_turn(
        state="awaiting_authorization",
        authorization={
            "calls": [{"tool_use_id": "t1", "name": "create_note",
                       "gated": True, "input_preview": "{}"}],
            "decision": None,
        },
    )
    monkeypatch.setattr(rc.conv_model, "get_turn", lambda c, t: waiting)
    response = web_rendu.get("/chat/c1/tour/000002/fragment")
    assert response.status_code == 286
    html = response.get_data(as_text=True)
    assert "Autorisation requise" in html
    assert "Approuver" in html and "Refuser" in html


# ── The polling contract (the repo's first every-poll + 286) ───────────────

def test_pending_fragment_polls_and_terminal_answers_286(web_rendu, monkeypatch):
    pending = _final_turn(
        state="pending", segments=[],
        continuation={"token": "tok", "enqueued": True},
    )
    monkeypatch.setattr(rc.conv_model, "get_turn", lambda c, t: pending)
    response = web_rendu.get("/chat/c1/tour/000002/fragment")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'hx-trigger="every 2s"' in html
    assert "réflexion…" in html

    monkeypatch.setattr(rc.conv_model, "get_turn", lambda c, t: _final_turn())
    response = web_rendu.get("/chat/c1/tour/000002/fragment")
    assert response.status_code == 286          # htmx stops polling AND swaps
    assert "note-content" in response.get_data(as_text=True)


def test_pending_source_pins_the_poll_attributes():
    source = (_TEMPLATES / "chat" / "_pending.html").read_text(encoding="utf-8")
    assert 'hx-trigger="every 2s"' in source
    assert 'hx-swap="outerHTML"' in source


def test_message_post_races_are_refused_in_2xx_discipline(web_rendu, monkeypatch):
    monkeypatch.setattr(rc.conv_model, "get_conversation", lambda i: _conv())
    monkeypatch.setattr(
        rc.conv_model, "start_turn",
        lambda cid, text, **kw: (None, ["Un tour est déjà en cours …"]),
    )
    response = web_rendu.post("/chat/c1/message", data={"message": "Allo"})
    assert response.status_code == 302          # PRG — never a 4xx fragment
    assert "erreur=en_cours" in response.headers["Location"]


# ── Nav pins (both renders) ────────────────────────────────────────────────

def test_nav_carries_assistant_twice_with_the_forum_icon():
    source = (_TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert source.count('href="/chat"') == 2    # sidebar + mobile sheet
    assert source.count("ms('forum'") == 2


# ── The no-delete static sweep (SPEC §13.5, UI half) ───────────────────────

def test_no_delete_route_verb_or_affordance_exists():
    delete_verbs = re.compile(
        r"(supprimer|/delete|DELETE\"|methods=\[.*DELETE)", re.IGNORECASE
    )
    for path in [Path(_ATHENA) / "routes" / "chat.py"] + sorted(
        (_TEMPLATES / "chat").glob("*.html")
    ):
        source = path.read_text(encoding="utf-8")
        assert not delete_verbs.search(source), path
    # And the blueprint's live url_map: no DELETE method, no delete segment.
    app = Flask(__name__)
    app.register_blueprint(rc.chat_bp)
    for rule in app.url_map.iter_rules():
        assert "DELETE" not in (rule.methods or set()), rule
        assert "delete" not in rule.rule and "supprimer" not in rule.rule, rule


# ── The closed recurrence vocabulary (§12.1) ───────────────────────────────

def test_task_form_offers_exactly_the_three_recurrences(web_rendu, monkeypatch):
    monkeypatch.setattr(rc.skill_model, "list_skills", lambda: [])
    html = web_rendu.get("/chat/taches-planifiees/nouvelle").get_data(as_text=True)
    options = re.findall(
        r'<select id="recurrence"[^>]*>(.*?)</select>', html, re.DOTALL
    )
    assert options
    values = re.findall(r'value="([^"]+)"', options[0])
    assert values == ["quotidien", "jours_ouvrables", "hebdomadaire"]
    assert "cron" not in options[0]


# ── Drafts + skills screens smoke ──────────────────────────────────────────

def test_draft_detail_renders_history_and_verser_button(web_rendu, monkeypatch):
    draft = {
        "id": "b1", "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay", "title": "Projet de lettre",
        "content": "# Projet\n\nCorps.", "content_length": 18,
        "current_version": 2, "created_at": _NOW, "updated_at": _NOW,
    }
    monkeypatch.setattr(rc.draft_model, "get_draft", lambda i: draft)
    monkeypatch.setattr(
        rc.draft_model, "list_versions",
        lambda i, limit=50: [
            {"version": 2, "created_at": _NOW,
             "provenance": {"created_via": "chat", "conversation_id": "c1"}},
            {"version": 1, "created_at": _NOW,
             "provenance": {"created_via": "connector"}},
        ],
    )
    html = web_rendu.get("/chat/brouillons/b1").get_data(as_text=True)
    assert "Verser en Word" in html
    assert "Historique des versions (2)" in html
    assert "lecture seule" in html


def test_skills_list_states_the_no_delete_philosophy(web_rendu, monkeypatch):
    monkeypatch.setattr(rc.skill_model, "list_skills", lambda: [])
    html = web_rendu.get("/chat/competences").get_data(as_text=True)
    assert "la suppression, non" in html


# ── Reference files on the skill screens ───────────────────────────────────

_SKILL = {
    "id": "s1", "name": "Rédaction", "description": "", "body": "corps",
    "active": True, "current_version": 2,
    "files": [
        {"name": "Guide", "description": "Style.", "sha256": "a" * 64,
         "chars": 12}
    ],
    "created_at": _NOW, "updated_at": _NOW, "etag": "e",
}


def test_skill_form_carries_json_seed_and_single_hidden_files_field(
    web_rendu,
):
    html = web_rendu.get("/chat/competences/nouvelle").get_data(as_text=True)
    assert 'id="competence-fichiers-initial"' in html
    assert html.count('name="files_json"') == 1
    # Row controls carry NO name attributes — the hidden field is the ONE
    # serialization channel (the budgets lines_json pattern).
    assert "Retirer" in html
    assert "Importer un fichier texte" in html
    assert "Ajouter un fichier" in html


def test_skill_edit_seeds_current_file_contents(web_rendu, monkeypatch):
    monkeypatch.setattr(rc.skill_model, "get_skill", lambda i: dict(_SKILL))
    seeded = {}

    def _fake_contents(skill_id, manifest):
        seeded["manifest"] = manifest
        return [
            {"name": "Guide", "description": "Style.", "sha256": "a" * 64,
             "chars": 12, "content": "Contenu seed", "missing": False}
        ]

    monkeypatch.setattr(rc.skill_model, "list_file_contents", _fake_contents)
    html = web_rendu.get("/chat/competences/s1/modifier").get_data(as_text=True)
    assert seeded["manifest"] == _SKILL["files"]
    assert "Contenu seed" in html


def test_skill_edit_seed_escapes_script_close(web_rendu, monkeypatch):
    # Risk-6a pin: a file content containing a script-closing tag must not
    # break out of the non-executable JSON seed block.
    hostile = "avant </script><script>alert(1)</script> après"
    monkeypatch.setattr(rc.skill_model, "get_skill", lambda i: dict(_SKILL))
    monkeypatch.setattr(
        rc.skill_model,
        "list_file_contents",
        lambda i, m: [
            {"name": "Guide", "description": "", "sha256": "a" * 64,
             "chars": len(hostile), "content": hostile, "missing": False}
        ],
    )
    html = web_rendu.get("/chat/competences/s1/modifier").get_data(as_text=True)
    seed_start = html.index("competence-fichiers-initial")
    seed_region = html[seed_start : html.index("</script>", seed_start)]
    # tojson escapes < and > to \u003c/\u003e — the raw sequence is absent
    # from the seed block, the escaped one present.
    assert "</script><script>" not in seed_region
    assert "\\u003c/script\\u003e" in seed_region


def test_skill_create_passes_parsed_files_and_rerenders_errors(
    web_rendu, monkeypatch
):
    captured = {}

    def _fake_create(data):
        captured.update(data)
        return None, ["Le fichier « Guide » est vide."]

    monkeypatch.setattr(rc.skill_model, "create_skill", _fake_create)
    rows = [{"name": "Guide", "description": "", "content": ""}]
    resp = web_rendu.post(
        "/chat/competences",
        data={
            "name": "Rédaction",
            "description": "",
            "body": "corps",
            "files_json": _json.dumps(rows),
        },
    )
    # French model error re-renders the form in 200 (existing shape) with
    # the submitted rows re-seeded — the user's work survives.
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "est vide" in html
    assert captured["files"] == rows
    # Malformed JSON → the parse refusal, nothing reaches the model.
    resp = web_rendu.post(
        "/chat/competences",
        data={"name": "X", "body": "c", "files_json": "{pas-du-json"},
    )
    assert resp.status_code == 200
    assert "rechargez" in resp.get_data(as_text=True)


def test_skill_detail_renders_files_autoescaped(web_rendu, monkeypatch):
    monkeypatch.setattr(rc.skill_model, "get_skill", lambda i: dict(_SKILL))
    monkeypatch.setattr(rc.skill_model, "list_versions", lambda i, limit=50: [])
    monkeypatch.setattr(
        rc.skill_model,
        "list_file_contents",
        lambda i, m: [
            {"name": "Guide", "description": "Style.", "sha256": "a" * 64,
             "chars": 12, "content": "corps <b>x</b>", "missing": False}
        ],
    )
    html = web_rendu.get("/chat/competences/s1").get_data(as_text=True)
    assert "Fichiers de référence (1)" in html
    # Autoescape only — never |safe/|markdown on reference material.
    assert "corps &lt;b&gt;x&lt;/b&gt;" in html
    assert "corps <b>x</b>" not in html


# ── Observability parity (the test_logging_setup Literal precedent) ────────

def test_chat_event_vocabulary_and_emissions_agree_both_ways():
    from typing import get_args

    from utils.logging_setup import ChatEvent

    declared = set(get_args(ChatEvent))
    emitted: set[str] = set()
    sources = [
        Path(_ATHENA) / "routes" / "chat.py",
        Path(_ATHENA) / "routes" / "taches_chat.py",
        Path(_ATHENA) / "chat" / "turn_engine.py",
        Path(_ATHENA) / "chat" / "planification.py",
        Path(_ATHENA) / "chat" / "executors.py",
    ]
    for path in sources:
        emitted |= set(
            re.findall(
                r'log_chat_event\(\s*\n?\s*"([a-z_]+)"',
                path.read_text(encoding="utf-8"),
            )
        )
    # Every emission is declared (a typo would emit an unfilterable event)…
    assert emitted <= declared, f"non déclarés : {sorted(emitted - declared)}"
    # …and every declared event has an emitter (a dead vocabulary entry is
    # documentation that lies).
    assert declared <= emitted, f"jamais émis : {sorted(declared - emitted)}"
