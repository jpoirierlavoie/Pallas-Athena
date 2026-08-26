"""WSGI entrypoint of the « chat » service (chat.yaml).

ONLY the worker blueprint exists in this process (Phase N §2 — the UI
stays on the default service).
"""

from chat.app import create_chat_app

app = create_chat_app()
