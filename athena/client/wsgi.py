"""WSGI entrypoint for the « portail » App Engine service.

ONLY the portal blueprint exists in this process (spec L1 §2.1) — the
gunicorn entrypoint in portail.yaml targets ``client.wsgi:app``.
"""

from client.app import create_portail_app

app = create_portail_app()
