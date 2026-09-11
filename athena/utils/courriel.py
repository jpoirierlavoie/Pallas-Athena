"""Outbound email via Microsoft Graph sendMail (portail client, spec L1 §8.1).

One function, one responsibility. ``saveToSentItems: true`` keeps a copy of
every invitation and accusé in the juriste's « Éléments envoyés » folder —
the natural evidentiary trail. Failures raise (GraphError); retry semantics
belong to the caller (Cloud Tasks for the accusé path, the UI for emission).
"""

from typing import Optional

from config import Config
from utils import graph


def envoyer(
    destinataire: str,
    objet: str,
    corps_html: str,
    *,
    expediteur_nom: Optional[str] = None,
) -> None:
    """Send an HTML email from the configured sender mailbox.

    Raises GraphNotConfigured when the GRAPH_* configuration is absent and
    GraphError on any HTTP failure — never swallows.

    *expediteur_nom* is the « Paramètres → Intégrations » display name.
    ``None`` means « read ``Config`` », which is byte-for-byte the historical
    behaviour — and it is what keeps this module PURE: it never imports
    ``models``, so ``tests/test_graph_courriel.py`` (which does NOT mock
    ``firestore.Client``) cannot be made to construct one. The three callers
    that have a request context resolve the value and pass it.

    ⚠ The SENDER MAILBOX (``GRAPH_SENDER_UPN``) is deliberately NOT a
    parameter: it is gated by Exchange RBAC scope that ``graph-client-secret``
    cannot administer, so repointing it from a web form would answer 403 on
    every send. Any future lot that makes it editable must ALSO key
    ``utils/graph``'s token cache on the credential triple in the same commit,
    or a worker serves an old-tenant token for up to 55 minutes and the page
    appears to have done nothing.
    """
    # « from » explicite = l'expéditeur affiché est bien reception@…
    # (GRAPH_SENDER_UPN), même si c'est un alias d'une autre boîte : la boîte
    # détient le « Send As » sur ses propres alias, donc pas de permission
    # supplémentaire requise. Le « name » (GRAPH_SENDER_NAME) remplace le nom
    # d'annuaire de la boîte hôte dans la boîte de réception du client ; omis
    # quand il n'est pas configuré, pour garder la forme historique.
    nom = Config.GRAPH_SENDER_NAME if expediteur_nom is None else expediteur_nom
    expediteur: dict = {"address": Config.GRAPH_SENDER_UPN}
    if nom:
        expediteur["name"] = nom
    graph.graph_post(
        f"/users/{Config.GRAPH_SENDER_UPN}/sendMail",
        {
            "message": {
                "subject": objet,
                "body": {"contentType": "HTML", "content": corps_html},
                "from": {"emailAddress": expediteur},
                "toRecipients": [{"emailAddress": {"address": destinataire}}],
            },
            # Copie dans les « Éléments envoyés » de la boîte (trace probante).
            "saveToSentItems": True,
        },
    )
