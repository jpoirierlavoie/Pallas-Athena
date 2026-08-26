"""Chat IA (Phase N) — Claude on Vertex AI, inside Pallas Athéna.

The package holds the chat client's own machinery:

* ``charter.py``      — the versioned French system charter (+ the unattended
                        addendum) and the system-block assembly.
* ``registry.py``     — the chat-side tool registry: which tools the model
                        sees, by which executor each one runs. Internal tools
                        REFERENCE ``mcp.tools.TOOLS`` entries by identity —
                        schemas are never copied.
* ``executors.py``    — tool execution for the turn worker: in-process calls
                        into ``mcp/handlers.py`` (with the endpoint-side
                        validation reproduced), HTTP calls to the legal
                        Workers. A tool failure becomes an error tool_result;
                        nothing here ever raises out of a turn.
* ``worker_tools.py`` — the legislation/jurisprudence Worker tool specs
                        (data, late-bound).
* ``worker_client.py``— the bounded HTTP client for those Workers.
* ``vertex.py``       — the raw-``requests`` Messages API client on Vertex.
* ``turn_engine.py``  — claim → work → commit orchestration of a turn.
* ``taches.py``       — Cloud Tasks enqueue onto the ``chat-turns`` queue
                        (importable by BOTH services, like
                        ``client/services/taches.py``).
* ``planification.py``— scheduled-task due computation + dispatch.
* ``app.py``/``wsgi.py`` — the dedicated « chat » App Engine service
                        (worker blueprint only; see chat.yaml).

Import discipline: unlike the portail package, chat modules MAY import
``models``/``config`` — the chat service runs under the DEFAULT service
account with the default service's env block, so eager secret resolution and
the default-database Firestore client are both legitimate here (the portail
ban was a property of its least-privilege SA, not of being a second
service). What stays lazy is anything Firestore-touching inside modules that
pure tests import (``registry``/``executors`` import ``mcp.tools`` only;
handlers and models load at call time via ``mcp.tools.get_handler``).

No delete verb exists anywhere in this package, by design (SPEC §1).
"""
