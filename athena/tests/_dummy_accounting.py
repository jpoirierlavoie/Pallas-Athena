"""A DUMMY accounting tool, registered for the length of ONE test.

The ``athena:comptabilite`` scope shipped DORMANT (plan lot 0a): no real
tool carried it before lot 5, so ``mcp.tools.ACCOUNTING_TOOLS`` was empty and
every gate that set feeds — the tools/list filter, the tools/call refusal,
the write-time token revalidation, the consent box and its grant — would
have been tested VACUOUSLY. A gate that has never seen a member is a gate
nobody has seen close. Since lot 5b six real tools carry the scope; the
dummy stays because it lets a test observe the DISPATCH (a recorder handler)
without a register behind it.

:func:`register` puts one fake tool under the scope, through
``monkeypatch``, exactly the way a lot-5 tool will arrive: a declared
``"scope": SCOPE_COMPTABILITE``, a member of ``WRITE_TOOLS``, present in
``ACCOUNTING_TOOLS``, a declared ``outputSchema``, a handler resolved BY
NAME, and the ``required`` idempotency policy guard (g) demands of money
tools. Every gate is then exercised through the registry lookups production
uses — never a patched gate. ``monkeypatch`` restores the real registry when
the test ends, so no other test can see the tool.

Importing this module imports nothing from the app: callers have already
imported ``mcp.tools`` (and ``mcp.handlers``, when they pass a handler)
under their own ``firestore.Client`` patch.
"""

from typing import Callable, Optional

DUMMY_NAME = "zz_test_accounting_entry"
DUMMY_TITLE = "Écriture comptable fictive (test)"
DUMMY_HANDLER = "_zz_test_accounting_entry"


def register(monkeypatch, *, handler: Optional[Callable[[dict], dict]] = None) -> str:
    """Register the dummy accounting tool; return its name.

    *handler* is attached to ``mcp.handlers`` under the registered name only
    when given — a test that never dispatches (the consent screen, the pure
    registry) then does not import the handlers, and with them the models.
    A dispatching test passes a RECORDER: the recorder's calls prove a call
    reached the handler, and their absence proves a refusal came first.
    It should return ``{"recorded": True}`` to honour the declared output.
    """
    import mcp
    import mcp.tools as tools

    spec = {
        "title": DUMMY_TITLE,
        "description": "Test-only accounting write; never registered outside a test.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount_cents": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Test amount, in cents.",
                },
                **tools._write_protocol_props(),
            },
            "required": ["amount_cents", "idempotency_key"],
            "additionalProperties": False,
        },
        "scope": mcp.SCOPE_COMPTABILITE,
        "idempotency": tools.IDEMPOTENCY_REQUIRED,
        "handler": DUMMY_HANDLER,
    }
    monkeypatch.setitem(tools.TOOLS, DUMMY_NAME, spec)
    monkeypatch.setitem(tools.OUTPUT_SCHEMAS, DUMMY_NAME, {
        "type": "object",
        "properties": {"recorded": {"type": "boolean"}},
        "required": ["recorded"],
    })
    monkeypatch.setattr(tools, "WRITE_TOOLS", tools.WRITE_TOOLS | {DUMMY_NAME})
    monkeypatch.setattr(
        tools, "ACCOUNTING_TOOLS", tools.ACCOUNTING_TOOLS | {DUMMY_NAME}
    )
    if handler is not None:
        import mcp.handlers as handlers

        monkeypatch.setattr(handlers, DUMMY_HANDLER, handler, raising=False)
    return DUMMY_NAME


def call_args(**over) -> dict:
    """Arguments the dummy's input schema accepts."""
    args = {"amount_cents": 100, "idempotency_key": "cle-test-comptable-1"}
    args.update(over)
    return args
