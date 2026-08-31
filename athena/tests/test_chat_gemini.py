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
    for brut in ("SAFETY", "PROHIBITED_CONTENT"):
        assert gemini.parse_response(
            _reponse([{"text": "x"}], brut))["stop_reason"] == "refusal"
    # MALFORMED_FUNCTION_CALL a quitté le seau des refus le 2026-08-31 :
    # Gemini a mal formé SON PROPRE appel d'outil, ce qui est un raté
    # d'échantillonnage transitoire, pas une décision de politique. Rangé
    # avec SAFETY, il devenait fatal sans une seule reprise.
    assert gemini.parse_response(
        _reponse([{"text": "x"}], "MALFORMED_FUNCTION_CALL"),
    )["stop_reason"] == "malformed_tool_call"
    # Un motif INCONNU ne se lit plus « fini ». L'énumération de Vertex a
    # des membres que la table ignore (OTHER, LANGUAGE, TOO_MANY_TOOL_CALLS
    # …) : les blanchir en fin normale faisait passer un arrêt anormal pour
    # un tour complet, et une réponse PARTIELLE sous un tel motif se livrait
    # comme un rapport entier.
    assert gemini.parse_response(
        _reponse([{"text": "x"}], "ZORGLUB"))["stop_reason"] == "unknown"


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
    # « unknown », jamais « end_turn » : sans candidat il n'y a pas de motif
    # d'arrêt, et en inventer un de succès est ce qui a fait passer une
    # réponse absente pour un breffage terminé.
    assert out["stop_reason"] == "unknown"
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


# ── Les schémas d'outils, contre le corpus RÉEL ────────────────────────────
#
# « Les schémas passent verbatim » était vrai d'un ÉCHANTILLON des outils
# MCP, vérifié en direct le 2026-08-26. Ce n'était pas vrai du corpus : les
# fiches des Workers juridiques sont ENGENDRÉES depuis le `tools/list` de
# chaque Worker, donc elles portent ce que le Worker émet — `$schema`
# compris. Personne ne l'avait vu parce qu'aucun Worker n'était branché ce
# jour-là. Les 10 outils legislation_* ont fait échouer TOUS les tours
# Gemini — le modèle par défaut — dès leur mise en ligne, en 400
# INVALID_ARGUMENT « Unknown name "$schema" … Cannot find field ».
#
# D'où la forme de ce test : il balaie TOUTES les fiches que le dépôt peut
# offrir, jamais la sélection qu'une configuration de test active — et il
# procède par LISTE BLANCHE. Une clé inconnue échoue ici plutôt qu'en
# production, ce qu'une liste noire (« ce qu'on a déjà vu casser ») ne peut
# pas faire.

# Le sous-ensemble d'OpenAPI que `functionDeclarations.parameters` accepte.
# `additionalProperties` et `title` y sont : vérifiés en direct, HTTP 200.
_CLES_ACCEPTEES = frozenset({
    "type", "format", "title", "description", "nullable", "default",
    "items", "minItems", "maxItems", "enum", "properties", "required",
    "minProperties", "maxProperties", "minimum", "maximum",
    "minLength", "maxLength", "pattern", "example", "anyOf",
    "propertyOrdering", "additionalProperties",
})


def _toutes_les_fiches():
    """Chaque outil que le dépôt peut déclarer — MCP, Workers, chat-local.

    PAS `registry.anthropic_tools()` : celui-ci n'inclut les Workers que
    s'ils sont configurés, et l'environnement de test ne les configure pas.
    Un test qui lit la sélection active aurait été vert le jour où la
    production est tombée.
    """
    import chat.registry as registry
    import chat.worker_tools as worker_tools
    import mcp.tools as mcp_tools

    fiches = list(mcp_tools.TOOLS.values())
    fiches += list(worker_tools.WORKER_TOOLS)
    fiches.append(registry.GET_SKILL_FILE_SPEC)
    return fiches


def _cles(valeur, dedans_properties=False):
    """Les clés STRUCTURELLES du schéma — jamais les noms de propriétés."""
    trouvees = set()
    if isinstance(valeur, dict):
        for k, v in valeur.items():
            if not dedans_properties:
                trouvees.add(k)
            trouvees |= _cles(v, dedans_properties=(k == "properties"))
    elif isinstance(valeur, list):
        for v in valeur:
            trouvees |= _cles(v)
    return trouvees


def test_no_tool_schema_carries_a_key_gemini_refuses():
    fiches = _toutes_les_fiches()
    assert len(fiches) > 60, "le corpus n'a pas été chargé"
    declarations = gemini.function_declarations(fiches)
    assert len(declarations) == len(fiches)
    for d in declarations:
        inconnues = _cles(d["parameters"]) - _CLES_ACCEPTEES
        assert not inconnues, f"{d['name']} : {sorted(inconnues)}"


def test_the_filter_never_mutates_the_shared_schema():
    """Le schéma d'entrée est partagé PAR IDENTITÉ avec mcp.tools.TOOLS.

    Le filtrer en place l'amputerait aussi pour le connecteur externe et
    pour le chemin Anthropic, qui l'acceptent tous deux tel quel.
    """
    import copy

    fiches = _toutes_les_fiches()
    avant = copy.deepcopy([f.get("input_schema") for f in fiches])
    gemini.function_declarations(fiches)
    assert [f.get("input_schema") for f in fiches] == avant


def test_the_legislation_specs_still_declare_what_the_worker_emits():
    """Les fiches restent FIDÈLES : le filtre vit chez Gemini, pas chez
    elles. Le chemin Anthropic reçoit le schéma tel que le Worker l'a
    annoncé, et le connecteur externe aussi."""
    import chat.worker_tools as worker_tools

    legislation = [t for t in worker_tools.WORKER_TOOLS
                   if t["name"].startswith("legislation_")]
    assert legislation, "aucune fiche legislation_*"
    assert any("$schema" in _cles(t["input_schema"]) for t in legislation)


# ── Réponses sans candidat (incident du 2026-08-31) ─────────────────────────


def test_un_blocage_d_invite_devient_un_refus_et_non_une_fin_normale():
    """Un blocage au niveau de l'INVITE ne rend AUCUN candidat.

    Sans la lecture de `promptFeedback`, `finishReason` est absent, le défaut
    « STOP » s'applique, et un refus de filtre se présente comme un tour
    terminé normalement — ce qui est exactement la façon dont un breffage
    planifié s'est soldé par un courriel vide.
    """
    out = gemini.parse_response(
        {"promptFeedback": {"blockReason": "SAFETY"},
         "usageMetadata": {"promptTokenCount": 63335}}
    )
    assert out["content"] == []
    assert out["stop_reason"] == "refusal"
    assert out["raw_stop_reason"] == "SAFETY"


def test_aucun_candidat_ne_se_declare_pas_STOP():
    """Sans candidat il n'y a pas de motif d'arrêt : ne pas en inventer un.

    Le contenu reste vide, et c'est `vertex` qui tranche — ici en reprise,
    le vide sans blocage étant traité comme transitoire.
    """
    out = gemini.parse_response({"candidates": [],
                                 "usageMetadata": {"promptTokenCount": 10}})
    assert out["content"] == []
    assert out["raw_stop_reason"] == ""


def test_le_motif_brut_voyage_avec_la_reponse():
    """Deux motifs Gemini distincts se traduisent en « end_turn » : le
    journal doit pouvoir les distinguer après coup."""
    out = gemini.parse_response(
        {"candidates": [{"content": {"parts": [{"text": "bonjour"}]},
                         "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}
    )
    assert out["stop_reason"] == "end_turn"
    assert out["raw_stop_reason"] == "STOP"
    assert out["usage"]["output_tokens"] == 2
