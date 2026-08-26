"""chat/gemini.py — la traduction dans les deux sens.

Le pari du module est que le moteur de tour ne change pas d'une ligne :
il parle un modèle de blocs de type Anthropic, et ce traducteur l'y
ramène. Ces tests épinglent les deux sens, et surtout les endroits où la
traduction pourrait mentir en silence.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from chat import gemini  # noqa: E402
from config import Config  # noqa: E402


# ── L'URL et l'hôte ─────────────────────────────────────────────────────


def test_l_hote_regional_porte_son_prefixe():
    # Oublier le préfixe donne un 404 qui se lit à tort « modèle absent ».
    assert gemini.host_for("global", "aiplatform.googleapis.com") == (
        "aiplatform.googleapis.com"
    )
    assert gemini.host_for(
        "northamerica-northeast1", "aiplatform.googleapis.com"
    ) == "northamerica-northeast1-aiplatform.googleapis.com"


def test_l_url_vise_generate_content_jamais_le_streaming():
    url = gemini.endpoint_url(
        host="northamerica-northeast1-aiplatform.googleapis.com",
        project="p",
        location="northamerica-northeast1",
        model="gemini-2.5-pro",
    )
    assert url.endswith(":generateContent")
    assert "streamGenerateContent" not in url
    assert "/publishers/google/models/gemini-2.5-pro" in url


# ── Aller : blocs → Gemini ──────────────────────────────────────────────


def test_le_systeme_devient_une_instruction_et_les_outils_des_declarations():
    body = gemini.build_body(
        system=[{"type": "text", "text": "charte"},
                {"type": "text", "text": "compétence"}],
        messages=[{"role": "user", "content": [{"type": "text", "text": "bonjour"}]}],
        tools=[{"name": "get_dossier", "description": "d",
                "input_schema": {"type": "object", "properties": {}}}],
        max_tokens=1000,
    )
    assert body["systemInstruction"]["parts"][0]["text"] == "charte\n\ncompétence"
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "bonjour"}]}
    ]
    decls = body["tools"][0]["functionDeclarations"]
    assert [d["name"] for d in decls] == ["get_dossier"]
    # Le schéma passe VERBATIM — vérifié en direct contre les outils MCP
    # réels le 2026-08-26 (additionalProperties compris).
    assert decls[0]["parameters"] == {"type": "object", "properties": {}}


def test_un_outil_serveur_sans_schema_est_ecarte():
    # web_search est un outil SERVEUR d'Anthropic : Gemini a son propre
    # ancrage, qui n'est pas câblé. L'omettre est la seule réponse honnête.
    body = gemini.build_body(
        system=[], messages=[], tools=[
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
            {"name": "get_dossier", "description": "d", "input_schema": {"a": 1}},
        ],
        max_tokens=10,
    )
    noms = [d["name"] for d in body["tools"][0]["functionDeclarations"]]
    assert noms == ["get_dossier"]


def test_le_role_assistant_devient_model():
    body = gemini.build_body(
        system=[],
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        ],
        tools=[], max_tokens=10,
    )
    assert [c["role"] for c in body["contents"]] == ["user", "model"]


def test_l_appel_et_son_resultat_se_traduisent():
    body = gemini.build_body(
        system=[],
        messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "get_dossier#1",
                 "name": "get_dossier", "input": {"dossier_id": "d1"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "get_dossier#1",
                 "content": [{"type": "text", "text": '{"ok": true}'}],
                 "is_error": False}]},
        ],
        tools=[], max_tokens=10,
    )
    appel = body["contents"][0]["parts"][0]["functionCall"]
    assert appel == {"name": "get_dossier", "args": {"dossier_id": "d1"}}
    reponse = body["contents"][1]["parts"][0]["functionResponse"]
    # Gemini apparie par NOM : il se retrouve dans l'id synthétisé.
    assert reponse["name"] == "get_dossier"
    assert reponse["response"]["resultat"] == '{"ok": true}'


def test_le_pdf_natif_devient_inline_data():
    body = gemini.build_body(
        system=[],
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": "JVBERi0="}}]}],
        tools=[], max_tokens=10,
    )
    part = body["contents"][0]["parts"][0]["inlineData"]
    assert part == {"mimeType": "application/pdf", "data": "JVBERi0="}


def test_les_blocs_muets_sont_ecartes_et_un_message_vide_disparait():
    """Gemini refuse un ``content`` sans ``parts`` : un message dont tous
    les blocs sont muets doit être OMIS, pas envoyé vide."""
    body = gemini.build_body(
        system=[],
        messages=[
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "…", "signature": "sig"}]},
            {"role": "user", "content": [{"type": "text", "text": "suite"}]},
        ],
        tools=[], max_tokens=10,
    )
    assert body["contents"] == [{"role": "user", "parts": [{"text": "suite"}]}]


def test_le_budget_de_reflexion_n_expose_jamais_les_pensees():
    # Sans invariant de rejeu byte-exact, afficher une réflexion qu'on ne
    # peut pas restituer fidèlement au tour suivant serait une promesse
    # qu'on ne tient pas.
    body = gemini.build_body(
        system=[], messages=[], tools=[], max_tokens=1000, thinking_budget=256
    )
    cfg = body["generationConfig"]["thinkingConfig"]
    assert cfg == {"thinkingBudget": 256, "includeThoughts": False}
    # Sans budget déclaré, aucune clé de réflexion n'est envoyée.
    nu = gemini.build_body(system=[], messages=[], tools=[], max_tokens=10)
    assert "thinkingConfig" not in nu["generationConfig"]


# ── Retour : Gemini → blocs ─────────────────────────────────────────────


def _reponse(parts, finish="STOP", meta=None):
    return {
        "candidates": [{"content": {"parts": parts}, "finishReason": finish}],
        "usageMetadata": meta or {"promptTokenCount": 10, "candidatesTokenCount": 3},
    }


def test_le_texte_revient_en_bloc_texte():
    out = gemini.parse_response(_reponse([{"text": "Voici."}]))
    assert out["content"] == [{"type": "text", "text": "Voici."}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"]["input_tokens"] == 10
    assert out["usage"]["output_tokens"] == 3


def test_un_appel_d_outil_force_le_stop_reason_tool_use():
    """Gemini rend « STOP » alors même qu'il appelle un outil ; le moteur
    attend « tool_use » pour continuer la chaîne. Sans cette conversion,
    TOUT tour outillé se terminerait après un seul appel."""
    out = gemini.parse_response(
        _reponse([{"functionCall": {"name": "get_dossier", "args": {"a": 1}}}])
    )
    assert out["stop_reason"] == "tool_use"
    assert out["content"] == [
        {"type": "tool_use", "id": "get_dossier#1",
         "name": "get_dossier", "input": {"a": 1}}
    ]


def test_deux_appels_du_meme_outil_recoivent_des_ids_distincts():
    """Le piège que le seul nom aurait posé : Gemini peut appeler deux
    fois le même outil dans un tour, et deux ids identiques colleraient
    les deux résultats l'un sur l'autre."""
    out = gemini.parse_response(_reponse([
        {"functionCall": {"name": "get_dossier", "args": {"a": 1}}},
        {"functionCall": {"name": "get_dossier", "args": {"a": 2}}},
    ]))
    ids = [b["id"] for b in out["content"]]
    assert ids == ["get_dossier#1", "get_dossier#2"]
    assert len(set(ids)) == 2


def test_l_aller_retour_d_un_appel_preserve_le_nom():
    out = gemini.parse_response(_reponse([
        {"functionCall": {"name": "set_expense_phase", "args": {}}},
    ]))
    body = gemini.build_body(
        system=[],
        messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": out["content"][0]["id"],
             "content": [{"type": "text", "text": "ok"}]}]}],
        tools=[], max_tokens=10,
    )
    assert body["contents"][0]["parts"][0]["functionResponse"]["name"] == (
        "set_expense_phase"
    )


def test_les_motifs_d_arret_se_traduisent():
    assert gemini.parse_response(
        _reponse([{"text": "x"}], "MAX_TOKENS"))["stop_reason"] == "max_tokens"
    for brut in ("SAFETY", "PROHIBITED_CONTENT", "MALFORMED_FUNCTION_CALL"):
        assert gemini.parse_response(
            _reponse([{"text": "x"}], brut))["stop_reason"] == "refusal"
    # Un motif inconnu ne fait pas exploser le tour : il se lit « fini ».
    assert gemini.parse_response(
        _reponse([{"text": "x"}], "ZORGLUB"))["stop_reason"] == "end_turn"


def test_un_refus_prime_sur_l_appel_d_outil():
    out = gemini.parse_response(_reponse(
        [{"functionCall": {"name": "get_dossier", "args": {}}}], "SAFETY"))
    assert out["stop_reason"] == "refusal"


def test_les_jetons_de_reflexion_comptent_en_sortie():
    """Les omettre sous-estimerait la dépense EN SILENCE, ce que la
    comptabilité du registre ne doit jamais faire."""
    out = gemini.parse_response(_reponse([{"text": "x"}], meta={
        "promptTokenCount": 100, "candidatesTokenCount": 20,
        "thoughtsTokenCount": 500, "cachedContentTokenCount": 7,
    }))
    assert out["usage"]["output_tokens"] == 520
    assert out["usage"]["cache_read_input_tokens"] == 7


def test_les_pensees_ne_reviennent_jamais_dans_le_contenu():
    out = gemini.parse_response(_reponse([
        {"text": "raisonnement interne", "thought": True},
        {"text": "réponse"},
    ]))
    assert out["content"] == [{"type": "text", "text": "réponse"}]


def test_une_reponse_vide_reste_une_forme_valide():
    # Aucun candidat : le moteur doit recevoir un contrat, pas une
    # exception — c'est lui qui décide de la terminalisation.
    out = gemini.parse_response({})
    assert out["content"] == []
    assert out["stop_reason"] == "end_turn"
    assert out["usage"]["input_tokens"] == 0


# ── Le contrat que le transport valide ──────────────────────────────────


def test_la_sortie_satisfait_le_contrat_du_moteur():
    """vertex.call_model valide la MÊME forme pour les deux fournisseurs :
    une validation unique, donc pas de chemin où l'une passe et l'autre
    non."""
    out = gemini.parse_response(_reponse([{"text": "x"}]))
    assert isinstance(out, dict)
    assert isinstance(out["content"], list)
    assert "stop_reason" in out
    assert isinstance(out["usage"], dict)


def test_la_tarification_couvre_les_modeles_google():
    for key, cfg in Config.CHAT_MODELS.items():
        if cfg["provider"] != Config.PROVIDER_GOOGLE:
            continue
        tarifs = Config.CHAT_PRICING["models"][key]
        assert tarifs["input_usd_per_mtok"] > 0, key
        assert tarifs["output_usd_per_mtok"] > 0, key
