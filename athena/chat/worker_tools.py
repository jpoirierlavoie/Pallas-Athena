"""Legal-research Worker tool specs — GENERATED. Do not edit by hand.

Regenerate with ``python -m scripts.sync_worker_tools --worker <name>
--url <base>``; check for drift with the same command plus ``--check``.
The generator is the only writer of this file, and it is the sole reason
nothing here can silently disagree with the Workers.

The two Cloudflare Workers (``legislation``, ``jurisprudence``) are MCP
servers: one JSON-RPC ``tools/call`` per invocation, over ``POST /mcp``,
authenticated by a bearer token each (user decisions D5/D10 — D5 amended
2026-08-26: the Workers speak MCP, not plain REST).

Entry shape (one dict per tool):

    {
        "name": "jurisprudence_canlii_verify_citations",  # offered to the model
        "title": "Vérifier des citations",                # French display title
        "description": "…",                               # VERBATIM from the Worker
        "input_schema": {…},                              # VERBATIM from the Worker
        "worker": "jurisprudence",                        # which token/URL pair
        "tool": "canlii_verify_citations",                # the REMOTE name
        "path": "/mcp",                                   # appended to the Worker URL
        "method": "POST",
        "transport": "mcp",
    }

Rules, pinned by tests/test_chat_registry.py:

* every ``name`` matches ``^(legislation|jurisprudence)_`` and is DISJOINT
  from ``mcp.tools.TOOLS`` — these tools live in the CHAT registry only and
  never reach the external MCP connector (claude.ai already talks to the
  Workers directly; re-exporting them would double the surface for nothing);
* ``worker`` ∈ {"legislation", "jurisprudence"} — it selects the URL and the
  bearer token (two DISTINCT secrets, independent revocation);
* ``name`` is the model-facing name and ``tool`` the REMOTE one; the client
  sends ``tool``, never ``name`` (chat/worker_client.py);
* ``description`` and ``input_schema`` reach the model verbatim. The Worker
  owns its own validation — the chat client only bounds sizes.

ORDER IS THE SERVER'S ORDER and must stay stable: the model's tools array
is the prompt-cache prefix, and a reshuffle costs a full cache miss on
every turn.
"""

from __future__ import annotations

WORKER_NAME_PREFIXES: tuple[str, ...] = ("legislation_", "jurisprudence_")

WORKER_TOOLS: tuple[dict, ...] = (
    {
        "name": "jurisprudence_canlii_verify_citations",
        "title": "Vérifier des citations",
        "description": "Vérifie une ou plusieurs citations de jurisprudence contre la collection de CanLII. Pour chacune : un verdict (CONFIRMÉE, DISCORDANTE, INTROUVABLE, NON CONSTRUCTIBLE, ILLISIBLE), la fiche officielle (intitulé, citation, date, n° de dossier, hyperlien) et, s'il y a lieu, l'écart avec l'intitulé attendu. Établit l'EXISTENCE et l'IDENTITÉ d'une décision ; n'établit NI son autorité actuelle (aucun historique d'appel, aucun indicateur de traitement), NI le contenu de son dispositif. Outil de choix pour éprouver des références tirées de la doctrine, d'un moteur de recherche ou d'un texte rédigé par une IA. Les citations de recueils (R.C.S., R.J.Q., C.A.) et les identifiants d'éditeurs (J.E., REJB, EYB, AZ) ne sont pas résolubles directement : enchaîner avec canlii_find_case.",
        "input_schema": {
            "type": "object",
            "properties": {
                "citations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 25,
                    "description": "Les citations à éprouver, au plus 25 par appel.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "citation": {
                                "type": "string",
                                "maxLength": 400,
                                "description": "La citation telle qu'elle a été rencontrée. Une citation doctrinale complète est acceptée : l'analyseur y trouve la forme constructible.",
                            },
                            "expected_title": {
                                "type": "string",
                                "maxLength": 300,
                                "description": "Intitulé annoncé par la source, s'il est connu.",
                            },
                            "expected_year": {
                                "type": "integer",
                                "minimum": 1800,
                                "maximum": 2100,
                                "description": "Année annoncée par la source, si elle est connue.",
                            },
                        },
                        "required": ["citation"],
                        "additionalProperties": False,
                    },
                },
                "lang": {
                    "type": "string",
                    "enum": ["fr", "en"],
                    "description": "Langue de la collection interrogée : « fr » (défaut) ou « en ».",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "Forcer un appel à CanLII plutôt que de servir la fiche en cache.",
                },
            },
            "required": ["citations"],
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_verify_citations",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_canlii_find_case",
        "title": "Retrouver une décision par les parties",
        "description": "Recherche une décision par les noms des parties ou un fragment d'intitulé, avec tribunal et bornes de date facultatifs. Sert de rattrapage lorsque la citation n'est pas constructible (recueils, SOQUIJ) ou lorsqu'on ne connaît que les parties et l'année. Interroge d'abord l'index local, puis balaie la base de CanLII sur la fenêtre demandée. La recherche porte sur l'INTITULÉ et les mots-clés uniquement — l'API de CanLII n'expose pas le texte des décisions et ne permet aucune recherche par mots du texte.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 200,
                    "description": "Noms des parties ou fragment d'intitulé.",
                },
                "database_id": {
                    "type": "string",
                    "maxLength": 20,
                    "description": "Tribunal ciblé, p. ex. « qcca ». Voir canlii_list_databases.",
                },
                "year_from": {
                    "type": "integer",
                    "minimum": 1800,
                    "maximum": 2100,
                },
                "year_to": {
                    "type": "integer",
                    "minimum": 1800,
                    "maximum": 2100,
                },
                "lang": {
                    "type": "string",
                    "enum": ["fr", "en"],
                    "description": "Langue de la collection interrogée : « fr » (défaut) ou « en ».",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Nombre de candidats rendus (défaut 10, maximum 25).",
                },
                "live": {
                    "type": "boolean",
                    "description": "Balayer CanLII en plus de l'index local. Défaut : vrai lorsque l'index rend moins de trois candidats.",
                },
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_find_case",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_canlii_get_case",
        "title": "Fiche d'une décision",
        "description": "Fiche officielle d'une décision : intitulé, citation, date, numéro de dossier de cour, mots-clés et hyperlien canlii.ca. Accepte soit une citation (« 2020 QCCA 495 »), soit le couple database_id + case_id. Ne renvoie PAS le texte de la décision : suivre l'hyperlien.",
        "input_schema": {
            "type": "object",
            "properties": {
                "citation": {
                    "type": "string",
                    "maxLength": 400,
                },
                "database_id": {
                    "type": "string",
                    "maxLength": 20,
                },
                "case_id": {
                    "type": "string",
                    "maxLength": 60,
                },
                "lang": {
                    "type": "string",
                    "enum": ["fr", "en"],
                    "description": "Langue de la collection interrogée : « fr » (défaut) ou « en ».",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "Forcer un appel à CanLII plutôt que de servir la fiche en cache.",
                },
            },
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_get_case",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_canlii_citator",
        "title": "Citateur — listes brutes",
        "description": "Citateur : décisions citées PAR une décision (`cited`), décisions qui LA citent (`citing`), ou dispositions législatives qu'elle cite (`legislation`). Les listes sont brutes : elles n'indiquent aucun sens de traitement (suivi, distingué, infirmé). Pour les dispositions québécoises, enchaîner avec le connecteur « Législation du Québec » afin d'en lire le texte officiel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "citation": {
                    "type": "string",
                    "maxLength": 400,
                },
                "database_id": {
                    "type": "string",
                    "maxLength": 20,
                },
                "case_id": {
                    "type": "string",
                    "maxLength": 60,
                },
                "rel": {
                    "type": "string",
                    "enum": ["cited", "citing", "legislation"],
                    "description": "« cited » : ce que la décision cite. « citing » : ce qui la cite. « legislation » : les dispositions qu'elle cite.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100000,
                },
                "refresh": {
                    "type": "boolean",
                    "description": "Forcer un appel à CanLII plutôt que de servir la fiche en cache.",
                },
            },
            "required": ["rel"],
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_citator",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_canlii_subsequent_history",
        "title": "Sorts ultérieurs — indice heuristique",
        "description": "Indice heuristique de sorts ultérieurs : parmi les décisions qui citent la décision de départ, retient celles qui émanent d'une juridiction supérieure et dont l'intitulé ressemble au sien. NE REMPLACE PAS un citateur professionnel : n'indique pas si la décision a été infirmée, confirmée ou distinguée, et ne détecte ni les pourvois pendants, ni les refus de permission d'appeler, ni les désistements. À vérifier systématiquement à la source.",
        "input_schema": {
            "type": "object",
            "properties": {
                "citation": {
                    "type": "string",
                    "maxLength": 400,
                },
                "database_id": {
                    "type": "string",
                    "maxLength": 20,
                },
                "case_id": {
                    "type": "string",
                    "maxLength": 60,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                },
                "refresh": {
                    "type": "boolean",
                    "description": "Forcer un appel à CanLII plutôt que de servir la fiche en cache.",
                },
            },
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_subsequent_history",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_canlii_browse_cases",
        "title": "Décisions d'un tribunal",
        "description": "Liste les décisions d'un tribunal, les plus récemment diffusées en tête, avec filtres de date : date de la décision (`decision_date_*`), date de diffusion sur CanLII (`published_*`) ou date de dernière modification (`modified_*`, `changed_*`). Utile pour la veille et pour cerner la couverture de CanLII pour un tribunal donné.",
        "input_schema": {
            "type": "object",
            "properties": {
                "database_id": {
                    "type": "string",
                    "maxLength": 20,
                },
                "lang": {
                    "type": "string",
                    "enum": ["fr", "en"],
                    "description": "Langue de la collection interrogée : « fr » (défaut) ou « en ».",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100000,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Nombre de fiches rendues (défaut 25, maximum 100). Bien en deçà du maximum de 10 000 de l'API : au-delà, la sortie est inexploitable par un modèle.",
                },
                "decision_date_after": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "Date au format AAAA-MM-JJ. Borne INCLUSIVE.",
                },
                "decision_date_before": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "Date au format AAAA-MM-JJ. Borne INCLUSIVE.",
                },
                "published_after": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "Date au format AAAA-MM-JJ. Borne INCLUSIVE.",
                },
                "published_before": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "Date au format AAAA-MM-JJ. Borne INCLUSIVE.",
                },
                "modified_after": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "Date au format AAAA-MM-JJ. Borne INCLUSIVE.",
                },
                "modified_before": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "Date au format AAAA-MM-JJ. Borne INCLUSIVE.",
                },
                "changed_after": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "Date au format AAAA-MM-JJ. Borne INCLUSIVE.",
                },
                "changed_before": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "Date au format AAAA-MM-JJ. Borne INCLUSIVE.",
                },
            },
            "required": ["database_id"],
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_browse_cases",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_canlii_list_databases",
        "title": "Répertoire des tribunaux et corpus",
        "description": "Répertoire des bases de CanLII : cours et tribunaux (`kind='case'`) ou corpus législatifs (`kind='legislation'`), avec leur databaseId et leur ressort. Point de départ de toute commande exigeant un database_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["case", "legislation"],
                },
                "jurisdiction": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "Ressort : « qc », « ca », « on »…",
                },
                "query": {
                    "type": "string",
                    "maxLength": 100,
                    "description": "Filtre sur le nom du tribunal.",
                },
                "lang": {
                    "type": "string",
                    "enum": ["fr", "en"],
                    "description": "Langue de la collection interrogée : « fr » (défaut) ou « en ».",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "Forcer un appel à CanLII plutôt que de servir la fiche en cache.",
                },
            },
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_list_databases",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_canlii_browse_legislation",
        "title": "Lois et règlements d'un corpus",
        "description": "Liste les lois ou règlements d'une base législative (p. ex. « qcs » pour les lois du Québec), avec leur legislationId, leur citation et leur type.",
        "input_schema": {
            "type": "object",
            "properties": {
                "database_id": {
                    "type": "string",
                    "maxLength": 20,
                },
                "lang": {
                    "type": "string",
                    "enum": ["fr", "en"],
                    "description": "Langue de la collection interrogée : « fr » (défaut) ou « en ».",
                },
                "query": {
                    "type": "string",
                    "maxLength": 100,
                    "description": "Filtre sur le titre.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100000,
                },
            },
            "required": ["database_id"],
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_browse_legislation",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_canlii_get_legislation",
        "title": "Fiche d'une loi ou d'un règlement",
        "description": "Fiche d'une loi ou d'un règlement : citation, type, régime de dates (entrée en vigueur), dates de début et de fin, indicateur d'abrogation et découpage en parties. Utile pour dater une disposition ou vérifier une abrogation. Pour le TEXTE d'une loi ou d'un règlement du Québec, utiliser le connecteur « Législation du Québec », qui rend le texte officiel verbatim.",
        "input_schema": {
            "type": "object",
            "properties": {
                "database_id": {
                    "type": "string",
                    "maxLength": 20,
                },
                "legislation_id": {
                    "type": "string",
                    "maxLength": 60,
                },
                "lang": {
                    "type": "string",
                    "enum": ["fr", "en"],
                    "description": "Langue de la collection interrogée : « fr » (défaut) ou « en ».",
                },
            },
            "required": ["database_id", "legislation_id"],
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_get_legislation",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_canlii_parse_citation",
        "title": "Analyser une citation (hors ligne)",
        "description": "Analyse une citation sans appeler CanLII : indique la forme reconnue (citation neutre, citation attribuée par CanLII, recueil, identifiant d'éditeur), et, si elle est constructible, le database_id et le case_id qui en découlent. Outil de diagnostic ; pour vérifier réellement l'existence d'une décision, utiliser canlii_verify_citations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "citation": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 400,
                },
            },
            "required": ["citation"],
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "canlii_parse_citation",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_greffe_parse_court_file_number",
        "title": "Numéro de dossier de cour du Québec (hors ligne)",
        "description": "Analyse un numéro de dossier de cour du Québec (NNN-NN-NNNNNN-NNN) et en tire le greffe — palais de justice et district judiciaire — puis la juridiction : tribunal, compétence, type de greffe. Un préfixe alphabétique (TAL, TAQ, C.F.…) désigne un tribunal administratif ou une cour fédérale, qui numérotent leurs dossiers eux-mêmes. Données de référence LOCALES, relevées auprès du ministère de la Justice du Québec : cet outil n'interroge ni CanLII ni aucun registre, et n'établit donc PAS que le dossier existe ou qu'il est actif. Les positions 7 et suivantes ne sont pas analysées ; aucune somme de contrôle n'est vérifiée.",
        "input_schema": {
            "type": "object",
            "properties": {
                "court_file_number": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 40,
                    "description": "Le numéro brut, p. ex. « 500-05-123456-241 » ou « TAL-594531 ».",
                },
            },
            "required": ["court_file_number"],
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "greffe_parse_court_file_number",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_palais_list",
        "title": "Palais de justice du Québec — répertoire",
        "description": "Répertorie les palais de justice et points de service de justice du Québec, avec leur adresse municipale, les numéros de greffe qui y siègent et leur district judiciaire. Filtrable par district, par type de lieu ou par texte libre (nom, ville, numéro de greffe). Relevé auprès du ministère de la Justice du Québec le 2026-07-15 : les adresses DÉMÉNAGENT, et ce connecteur ne porte aucune coordonnée téléphonique ni courriel. Vérifier la liste officielle du Ministère avant toute signification ou tout dépôt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "district": {
                    "type": "string",
                    "maxLength": 60,
                    "description": "District judiciaire, p. ex. « Montréal ». Les diacritiques sont pliés.",
                },
                "query": {
                    "type": "string",
                    "maxLength": 60,
                    "description": "Texte libre : nom du palais, ville ou numéro de greffe.",
                },
                "type": {
                    "type": "string",
                    "enum": ["palais", "point_de_service"],
                    "description": "« palais » (43) ou « point_de_service » (8, au sens du MJQ — à ne pas confondre avec les greffes de cour itinérante).",
                },
            },
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "palais_list",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
    {
        "name": "jurisprudence_palais_get",
        "title": "Palais de justice du Québec — fiche",
        "description": "Fiche d'un lieu de justice du Québec, par numéro de greffe (3 chiffres) OU par nom de palais : adresse municipale, adresse postale distincte le cas échéant, greffes qui y siègent, district judiciaire et, pour une cour itinérante, les localités desservies. Relevé LOCAL auprès du ministère de la Justice du Québec, sans appel sortant. Six greffes n'ont aucune adresse publiée : l'outil le dit sans jamais affirmer qu'il n'en existe pas. Aucune coordonnée téléphonique ni courriel n'est portée par ce connecteur.",
        "input_schema": {
            "type": "object",
            "properties": {
                "greffe_number": {
                    "type": "string",
                    "maxLength": 3,
                    "description": "Numéro de greffe à 3 chiffres, p. ex. « 500 ».",
                },
                "palais": {
                    "type": "string",
                    "maxLength": 60,
                    "description": "Nom ou clef du palais, p. ex. « Montréal » ou « saint-jerome ».",
                },
            },
            "additionalProperties": False,
        },
        "worker": "jurisprudence",
        "tool": "palais_get",
        "path": "/mcp",
        "method": "POST",
        "transport": "mcp",
    },
)
