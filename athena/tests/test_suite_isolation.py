"""The suite's order independence, pinned where it once broke (finitions, E3).

A test module that installs the production log chain
(``utils.logging_setup.init_app``) left it on the root logger for every
later module, and ``tests/test_dav_root_discovery.py`` — whose 503 tests
assert that the traceback rides along (``record.exc_info``) — then failed
four tests, deterministically, whenever it ran AFTER one of those modules:
in a reversed full run, or in exactly the order below. The default
alphabetical order hid it. ``tests/conftest.py`` removes the chain at the
end of the module that installed it.

The check runs the failing order in a SUBPROCESS: within this process the
order is pytest's, and a leak can only be observed across module
boundaries. Fails without tests/conftest.py (7817412, 75efb45).
"""

import os
import subprocess
import sys

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_a_module_that_installs_the_log_chain_leaks_it_to_no_later_module():
    order = [
        "tests/test_logging_setup.py",      # calls init_app directly
        "tests/test_portail_app.py",        # create_portail_app → init_app
        "tests/test_dav_root_discovery.py",  # asserts record.exc_info
    ]
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", *order],
        cwd=_ATHENA, capture_output=True, text=True, timeout=240,
        stdin=subprocess.DEVNULL,
    )
    tail = "\n".join((run.stdout or "").splitlines()[-15:])
    assert run.returncode == 0, tail
    assert "failed" not in tail, tail
