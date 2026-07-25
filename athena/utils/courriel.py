"""Outbound email via Microsoft Graph sendMail (portail client, spec L1 §8.1).

One function, one responsibility. ``saveToSentItems: true`` keeps a copy of
every invitation and accusé in the juriste's « Éléments envoyés » folder —
the natural evidentiary trail. Failures raise (GraphError); retry semantics
belong to the caller (Cloud Tasks for the accusé path, the UI for emission).
"""

from config import Config
from utils import graph


def envoyer(destinataire: str, objet: str, corps_html: str) -> None:
    """Send an HTML email from the configured sender mailbox.

    Raises GraphNotConfigured when the GRAPH_* configuration is absent and
    GraphError on any HTTP failure — never swallows.
    """
    graph.graph_post(
        f"/users/{Config.GRAPH_SENDER_UPN}/sendMail",
        {
            "message": {
                "subject": objet,
                "body": {"contentType": "HTML", "content": corps_html},
                "toRecipients": [{"emailAddress": {"address": destinataire}}],
            },
            "saveToSentItems": True,
        },
    )
