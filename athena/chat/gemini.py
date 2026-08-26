"""Traducteur Gemini (Vertex AI) — vers et depuis le modèle de blocs.

Pourquoi ce module existe. Le quota Vertex des modèles Anthropic est à
ZÉRO sur ce projet et les demandes d'augmentation ont été refusées — pour
TOUTE la famille : le seau de quota est ``anthropic-claude-sonnet``, qui
couvre Sonnet 4, 4.5, 4.6 et 5 indifféremment, si bien qu'aucune version
n'y échappe. Gemini, lui, est servi — et servi à
``northamerica-northeast1``, c'est-à-dire à **Montréal**. Il livre donc ce
que Vertex-Claude n'a jamais pu livrer : l'inférence en province, sans
transfert hors Québec.

Le pari de conception : **le moteur de tour ne change pas d'une ligne.**
``chat/turn_engine.py`` parle un modèle de blocs de type Anthropic
(``text``, ``tool_use``, ``tool_result``, ``document``) de bout en bout —
assemblage, déchargement en Storage, rejeu. Ce module traduit dans les
DEUX sens, si bien que le moteur ne sait pas quel fournisseur a répondu.
C'est possible parce que ``chat/vertex.py`` est transport seulement.

Ce qui ne se traduit PAS, et qui dégrade honnêtement :

* **Les signatures de réflexion.** Anthropic exige le rejeu byte-exact des
  blocs de réflexion ; Gemini n'a pas cet invariant. On ne demande donc
  pas ``includeThoughts`` : la réflexion a lieu et se facture, mais elle
  n'est ni affichée ni rejouée. Le transcript n'aura pas de section
  « Réflexion » sur un tour Gemini — c'est vrai, et c'est mieux qu'une
  section vide.
* **``pause_turn``** n'existe pas ici ; le cas ne se présente simplement
  jamais.
* **Les points d'arrêt de cache** (``cache_control``) sont un mécanisme
  Anthropic : ils sont ignorés, sans erreur.
* **``web_search``** est un outil serveur Anthropic. Gemini a son propre
  ancrage (``googleSearch``), qui n'a ni la même forme ni le même contrat
  de citations : il n'est PAS câblé ici. Un tour Gemini n'a donc pas de
  recherche web — le registre le dira, puisque l'outil est absent du
  tableau d'outils.

⚠ **Les identifiants d'appel d'outil.** Anthropic rend un ``id`` par
``tool_use`` et le ``tool_result`` le référence ; Gemini ne rend aucun id
et apparie par NOM. On synthétise donc un id déterministe ``{nom}#{rang}``
à l'aller, et on en extrait le nom au retour. Le rang est indispensable :
Gemini peut appeler DEUX FOIS le même outil dans un tour, et un id fondé
sur le seul nom collerait les deux résultats l'un sur l'autre.
"""

from __future__ import annotations

import json
from typing import Any, Optional

# Les motifs d'arrêt de Gemini, vers le vocabulaire du moteur.
_FINISH_REASONS = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    # Tout ce qui suit est un refus du modèle ou de ses filtres. Le moteur
    # traite « refusal » comme terminal et bruyant, ce qui est la bonne
    # posture : un tour bloqué doit se voir, jamais se deviner.
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "BLOCKLIST": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "SPII": "refusal",
    "IMAGE_SAFETY": "refusal",
    "MALFORMED_FUNCTION_CALL": "refusal",
}

_ID_SEPARATEUR = "#"


def endpoint_url(*, host: str, project: str, location: str, model: str) -> str:
    """L'URL ``generateContent`` — jamais ``streamGenerateContent`` (le
    moteur est non streamé par conception, SPEC §2)."""
    return (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:generateContent"
    )


def host_for(location: str, default_host: str) -> str:
    """L'hôte régional. ``global`` garde l'hôte neutre ; toute autre
    localisation prend le préfixe régional — une régression ici donne un
    404 qui se lit à tort comme « modèle absent »."""
    return default_host if location == "global" else f"{location}-{default_host}"


# ── Aller : blocs → Gemini ──────────────────────────────────────────────


def _partie_depuis_bloc(bloc: dict, rangs: dict) -> Optional[dict]:
    """Un bloc du moteur → une ``part`` Gemini, ou ``None`` s'il est muet."""
    type_ = bloc.get("type")

    if type_ == "text":
        texte = str(bloc.get("text") or "")
        return {"text": texte} if texte else None

    if type_ == "tool_use":
        nom = str(bloc.get("name") or "")
        rangs[nom] = rangs.get(nom, 0) + 1
        return {"functionCall": {"name": nom, "args": bloc.get("input") or {}}}

    if type_ == "tool_result":
        # Gemini apparie par NOM : on le retrouve dans l'id synthétisé.
        brut = str(bloc.get("tool_use_id") or "")
        nom = brut.split(_ID_SEPARATEUR, 1)[0] or "outil"
        return {
            "functionResponse": {
                "name": nom,
                "response": {"resultat": _texte_de_resultat(bloc)},
            }
        }

    if type_ == "document":
        source = bloc.get("source") or {}
        if source.get("type") == "base64":
            return {
                "inlineData": {
                    "mimeType": source.get("media_type", "application/pdf"),
                    "data": source.get("data", ""),
                }
            }
        return None

    # thinking, server_tool_use, storage_ref non réhydraté… : muets ici.
    return None


def _texte_de_resultat(bloc: dict) -> str:
    contenu = bloc.get("content")
    if isinstance(contenu, str):
        return contenu
    if isinstance(contenu, list):
        return "\n".join(
            str(c.get("text") or "")
            for c in contenu
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def _role(role: str) -> str:
    # Gemini n'a que « user » et « model ».
    return "model" if role == "assistant" else "user"


def contents_depuis_messages(messages: list[dict]) -> list[dict]:
    """Les messages du moteur → le tableau ``contents``.

    Un message dont toutes les parts sont muettes est OMIS : Gemini refuse
    un ``content`` sans ``parts``.
    """
    rangs: dict = {}
    contents: list[dict] = []
    for message in messages or []:
        brut = message.get("content")
        blocs = brut if isinstance(brut, list) else [{"type": "text", "text": brut}]
        parts = []
        for bloc in blocs:
            if not isinstance(bloc, dict):
                continue
            partie = _partie_depuis_bloc(bloc, rangs)
            if partie is not None:
                parts.append(partie)
        if parts:
            contents.append({"role": _role(message.get("role", "user")), "parts": parts})
    return contents


def function_declarations(tools: list[dict]) -> list[dict]:
    """Les outils du registre → ``functionDeclarations``.

    Les schémas d'entrée passent **verbatim** : vérifié en direct le
    2026-08-26 contre un échantillon des outils MCP réels (y compris
    ``additionalProperties``), tous acceptés. Les outils serveur d'Anthropic
    (``web_search``, qui n'a pas d'``input_schema``) sont écartés — Gemini a
    son propre ancrage, qui n'est pas câblé.
    """
    declarations = []
    for outil in tools or []:
        schema = outil.get("input_schema")
        if not schema:
            continue
        declarations.append(
            {
                "name": outil.get("name", ""),
                "description": outil.get("description", ""),
                "parameters": schema,
            }
        )
    return declarations


def build_body(
    *,
    system: list[dict],
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    thinking_budget: Optional[int] = None,
) -> dict[str, Any]:
    """Le corps ``generateContent`` complet."""
    instruction = "\n\n".join(
        str(bloc.get("text") or "")
        for bloc in (system or [])
        if isinstance(bloc, dict) and bloc.get("text")
    )
    generation: dict[str, Any] = {"maxOutputTokens": int(max_tokens)}
    if thinking_budget is not None:
        # includeThoughts reste FAUX : sans invariant de rejeu byte-exact,
        # afficher une réflexion qu'on ne peut pas restituer fidèlement au
        # tour suivant serait une promesse qu'on ne tient pas.
        generation["thinkingConfig"] = {
            "thinkingBudget": int(thinking_budget),
            "includeThoughts": False,
        }
    body: dict[str, Any] = {
        "contents": contents_depuis_messages(messages),
        "generationConfig": generation,
    }
    if instruction:
        body["systemInstruction"] = {"parts": [{"text": instruction}]}
    declarations = function_declarations(tools)
    if declarations:
        body["tools"] = [{"functionDeclarations": declarations}]
    return body


# ── Retour : Gemini → blocs ─────────────────────────────────────────────


def _usage(payload: dict) -> dict:
    meta = payload.get("usageMetadata") or {}
    entree = int(meta.get("promptTokenCount") or 0)
    sortie = int(meta.get("candidatesTokenCount") or 0)
    # Les jetons de réflexion se facturent en SORTIE : les omettre
    # sous-estimerait la dépense en silence, ce que la comptabilité du
    # registre ne doit jamais faire.
    sortie += int(meta.get("thoughtsTokenCount") or 0)
    return {
        "input_tokens": entree,
        "output_tokens": sortie,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": int(meta.get("cachedContentTokenCount") or 0),
    }


def parse_response(payload: dict) -> dict[str, Any]:
    """La réponse Gemini → la forme que le moteur consomme.

    Rend ``{"content": [blocs], "stop_reason": str, "usage": {...}}`` — le
    contrat que ``vertex._valider_reponse`` vérifie déjà, si bien que la
    validation de forme reste UNE seule.
    """
    candidats = payload.get("candidates") or []
    premier = candidats[0] if candidats else {}
    parts = ((premier.get("content") or {}).get("parts")) or []

    blocs: list[dict] = []
    rangs: dict = {}
    a_un_appel = False
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought"):
            # Réflexion : jamais restituée (voir le docstring du module).
            continue
        appel = part.get("functionCall")
        if appel:
            nom = str(appel.get("name") or "")
            rangs[nom] = rangs.get(nom, 0) + 1
            blocs.append(
                {
                    "type": "tool_use",
                    "id": f"{nom}{_ID_SEPARATEUR}{rangs[nom]}",
                    "name": nom,
                    "input": appel.get("args") or {},
                }
            )
            a_un_appel = True
            continue
        texte = part.get("text")
        if texte:
            blocs.append({"type": "text", "text": str(texte)})

    brut = str(premier.get("finishReason") or "STOP")
    stop = _FINISH_REASONS.get(brut, "end_turn")
    # Un appel d'outil prime : Gemini rend « STOP » avec un functionCall,
    # là où le moteur attend « tool_use » pour continuer la chaîne. Sans
    # cette ligne, tout tour outillé se terminerait après un seul appel.
    if a_un_appel and stop not in ("max_tokens", "refusal"):
        stop = "tool_use"

    return {"content": blocs, "stop_reason": stop, "usage": _usage(payload)}
