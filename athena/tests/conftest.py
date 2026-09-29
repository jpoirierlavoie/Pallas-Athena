"""Suite-wide isolation: no test module leaves the production log chain on
the root logger (finitions, E3 — the flake hunt).

``utils.logging_setup.init_app`` REMOVES every root handler, installs the
production one (ContextFilter + RedactionFilter) and sets the root level to
DEBUG. ``tests/test_logging_setup.py`` calls it directly, and
``client.app.create_portail_app`` calls it from the module-scoped app
fixtures of ``tests/test_portail_*.py``. Nothing restored the root logger,
so every LATER module logged through that chain — and ``RedactionFilter``
rewrites a record IN PLACE (it renders ``exc_info`` into a scrubbed
``exc_text`` and clears ``exc_info``) before ``caplog``'s handler sees it.
A test asserting ``record.exc_info`` then passed or failed by the order of
the files: run in reverse (or ``test_logging_setup.py`` /
``test_portail_app.py`` before ``test_dav_root_discovery.py``), four tests
of the latter failed, deterministically — green in the default order only
because « d » sorts before « l » and « p ».

The cleanup is MODULE-scoped on purpose: the portal fixtures build their
app once per module, and a module may rely on the chain it installed; what
must never happen is that the NEXT module inherits it. It removes only the
handlers carrying the production RedactionFilter — never pytest's own
capture handlers, which pytest adds and removes around every phase — and
puts the root level back. ``tests/test_suite_isolation.py`` pins it by
running the failing order in a subprocess.
"""

import logging

import pytest


def _is_production_handler(handler: logging.Handler) -> bool:
    """A handler ``utils.logging_setup.init_app`` built: it carries the
    RedactionFilter (matched by name — importing the module here would pull
    Flask into every collection)."""
    return any(
        type(f).__name__ == "RedactionFilter"
        and type(f).__module__.endswith("logging_setup")
        for f in handler.filters
    )


@pytest.fixture(autouse=True, scope="module")
def _no_module_leaves_the_production_log_chain():
    root = logging.getLogger()
    level = root.level
    yield
    for handler in list(root.handlers):
        if _is_production_handler(handler):
            root.removeHandler(handler)
    root.setLevel(level)
