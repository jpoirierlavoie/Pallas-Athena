"""chat/vertex.py — transport, taxonomy, pricing (Phase N)."""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from chat import vertex  # noqa: E402
from chat.vertex import ChatVertexFatal, ChatVertexRetryable  # noqa: E402
from config import Config  # noqa: E402


class _Response:
    def __init__(self, status_code=200, payload=None, text="corps"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


_VALID = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@pytest.fixture()
def transport(monkeypatch):
    recorded = {}
    monkeypatch.setattr(vertex, "_bearer_token", lambda: "jeton-adc")

    def _post(url, json=None, headers=None, timeout=None):
        recorded.update(url=url, body=json, headers=headers, timeout=timeout)
        return recorded.get("response", _Response(payload=_VALID))

    monkeypatch.setattr(vertex.requests, "post", _post)
    return recorded


def _call(**kwargs):
    return vertex.call_model(
        "claude-sonnet-5",
        system=kwargs.get("system", [{"type": "text", "text": "charte"}]),
        messages=kwargs.get("messages", [{"role": "user", "content": []}]),
        tools=kwargs.get("tools", []),
    )


def test_url_and_body_shape(transport):
    _call(tools=[{"name": "t", "description": "d", "input_schema": {}}])
    assert transport["url"] == (
        "https://aiplatform.googleapis.com/v1/projects/test-project/"
        "locations/global/publishers/anthropic/models/claude-sonnet-5:rawPredict"
    )
    body = transport["body"]
    # The model goes in the URL, NEVER the body; the version in the body.
    assert "model" not in body
    assert body["anthropic_version"] == "vertex-2023-10-16"
    assert body["thinking"]["type"] == "adaptive"
    assert body["output_config"]["effort"] == (
        Config.CHAT_MODELS["claude-sonnet-5"]["effort"]
    )
    assert transport["headers"]["Authorization"] == "Bearer jeton-adc"
    assert transport["timeout"] == (
        Config.CHAT_VERTEX_CONNECT_TIMEOUT_S,
        Config.CHAT_VERTEX_READ_TIMEOUT_S,
    )


def test_body_carries_no_removed_parameter(transport):
    """The 2026-08-26 repair, pinned in the negative.

    `thinking.budget_tokens` and every sampling parameter are REMOVED on
    this model generation and return a 400. The quota was zero from the day
    the code landed, so the wrong body was never sent and nothing went red —
    which is exactly why the guard has to be a test rather than a comment.
    """
    _call()
    body = transport["body"]
    assert "budget_tokens" not in body["thinking"]
    for removed in ("temperature", "top_p", "top_k"):
        assert removed not in body, removed


def test_thinking_display_is_explicit(transport):
    # The default became "omitted": left implicit, every thinking block
    # returns empty text and the transcript's « Réflexion » renders blank.
    _call()
    assert transport["body"]["thinking"]["display"] == "summarized"


def test_unknown_model_and_bad_effort_fail_preflight(monkeypatch):
    with pytest.raises(ChatVertexFatal) as excinfo:
        vertex.model_config("claude-fable-5")
    assert excinfo.value.reason == "unknown_model"
    monkeypatch.setitem(
        Config.CHAT_MODELS,
        "claude-sonnet-5",
        {**Config.CHAT_MODELS["claude-sonnet-5"], "effort": "enorme"},
    )
    with pytest.raises(ChatVertexFatal) as excinfo:
        vertex.model_config("claude-sonnet-5")
    assert excinfo.value.reason == "config_effort"


def test_every_allowlisted_model_is_completely_declared():
    """Dérivé, jamais listé : un modèle ajouté sans son bouton de
    profondeur échoue ici plutôt qu'au premier appel réel. La règle est
    PAR FOURNISSEUR — « effort » chez Anthropic, « thinking_budget » chez
    Google — parce que les deux surfaces de requête n'ont rien en commun.
    """
    for key, cfg in Config.CHAT_MODELS.items():
        assert cfg["provider"] in (
            Config.PROVIDER_ANTHROPIC,
            Config.PROVIDER_GOOGLE,
        ), key
        assert int(cfg["max_tokens"]) > 0, key
        assert cfg.get("location"), key
        assert cfg.get("vertex_model_id"), key
        assert key in Config.CHAT_PRICING["models"], key
        if cfg["provider"] == Config.PROVIDER_ANTHROPIC:
            assert cfg.get("effort") in Config.VERTEX_EFFORTS, key
        else:
            assert int(cfg["thinking_budget"]) < int(cfg["max_tokens"]), key


def test_le_modele_par_defaut_est_dans_l_allowlist():
    assert Config.CHAT_DEFAULT_MODEL in Config.CHAT_MODELS


def test_anthropic_reste_en_global_et_gemini_a_montreal():
    """La localisation est PAR MODÈLE, et c'est porteur juridiquement.

    Anthropic n'est servi qu'en « global » — hors Québec, donc transfert
    au sens de l'art. 17 Loi 25. Gemini répond à Montréal : l'inférence
    reste en province, et le transfert devient sans objet sur ce chemin.
    Un modèle Gemini basculé en « global » perdrait ce bénéfice EN
    SILENCE — d'où l'épingle.
    """
    for key, cfg in Config.CHAT_MODELS.items():
        if cfg["provider"] == Config.PROVIDER_ANTHROPIC:
            assert cfg["location"] == "global", key
        else:
            assert cfg["location"] == "northamerica-northeast1", key


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
def test_retryable_statuses(transport, status):
    transport["response"] = _Response(status)
    with pytest.raises(ChatVertexRetryable) as excinfo:
        _call()
    assert excinfo.value.reason == f"vertex_http_{status}"


@pytest.mark.parametrize(
    "status,reason",
    [(400, "vertex_invalid_request"), (401, "vertex_permission"),
     (403, "vertex_permission"), (404, "vertex_endpoint_absent")],
)
def test_fatal_statuses_carry_bounded_excerpt(transport, status, reason):
    transport["response"] = _Response(status, text="e" * 5000)
    with pytest.raises(ChatVertexFatal) as excinfo:
        _call()
    assert excinfo.value.reason == reason
    assert len(excinfo.value.excerpt) == 2000  # bounded, doc-only
    # str(exc) is the machine-stable reason — safe to cross a span.
    assert str(excinfo.value) == reason


def test_timeouts_and_connection_errors_are_retryable(monkeypatch):
    import requests as _requests

    monkeypatch.setattr(vertex, "_bearer_token", lambda: "t")
    monkeypatch.setattr(
        vertex.requests, "post", mock.Mock(side_effect=_requests.Timeout())
    )
    with pytest.raises(ChatVertexRetryable) as excinfo:
        _call()
    assert excinfo.value.reason == "vertex_timeout"


def test_malformed_success_body_is_fatal(transport):
    transport["response"] = _Response(200, {"pas": "une réponse"})
    with pytest.raises(ChatVertexFatal) as excinfo:
        _call()
    assert excinfo.value.reason == "vertex_bad_response"


def test_pricing_math_in_usd_micros():
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "server_tool_use": {"web_search_requests": 100},
    }
    micros = vertex.segment_cost_usd_micros(usage, "claude-sonnet-5")
    # (3 + 15) USD au tarif de base du global (multiplicateur 1.0) +
    # 100 recherches × 10 $/1000 — les recherches ne sont jamais multipliées.
    expected = int(round(((3.00 + 15.00) * 1.0 + 1.00) * 1_000_000))
    assert micros == expected
    # Unknown model → 0, honestly under-reporting rather than inventing.
    assert vertex.segment_cost_usd_micros(usage, "modele-inconnu") == 0


# ── Une réponse SANS contenu (incident du 2026-08-31) ───────────────────────
#
# Gemini a rendu zéro bloc, zéro jeton de sortie et « end_turn » au cinquième
# appel d'un breffage planifié. La forme était valide — c'est pourquoi la
# validation ci-dessus la laissait passer —, mais il n'y avait pas de réponse :
# le moteur a pris cela pour un tour terminé, aucune note n'a été déposée, et
# le juriste a reçu un courriel vide sans la moindre erreur nulle part.


def test_an_empty_content_list_is_retryable(transport):
    """Vide + motif ordinaire : transitoire, donc la file reprend l'étape.

    Le jeton d'étape n'est PAS consommé sur un retryable, si bien que la
    redélivraison refait simplement l'appel, bornée par
    CHAT_TASK_RETRY_TERMINAL — au bout de quoi le tour meurt BRUYAMMENT.
    """
    transport["response"] = _Response(
        200,
        {"content": [], "stop_reason": "end_turn",
         "usage": {"input_tokens": 63335, "output_tokens": 0}},
    )
    with pytest.raises(ChatVertexRetryable) as excinfo:
        _call()
    assert excinfo.value.reason == "vertex_empty_response"


def test_an_empty_refusal_is_fatal_not_retryable(transport):
    """Un refus est DÉTERMINISTE : le rejouer brûlerait les reprises pour
    rien, et retarderait l'échec visible que l'on veut au plus tôt."""
    transport["response"] = _Response(
        200,
        {"content": [], "stop_reason": "refusal",
         "usage": {"input_tokens": 10, "output_tokens": 0}},
    )
    with pytest.raises(ChatVertexFatal) as excinfo:
        _call()
    assert excinfo.value.reason == "vertex_refusal"


def test_a_response_with_content_still_passes(transport):
    """La garde ne mord que sur le vide — le chemin nominal est intact."""
    transport["response"] = _Response(200, _VALID)
    assert _call()["content"] == [{"type": "text", "text": "ok"}]
