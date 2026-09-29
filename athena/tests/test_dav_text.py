"""utils.dav_text.remove_served_block — pure (finitions, sync-7)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import dav_text  # noqa: E402
from utils.dav_text import remove_served_block  # noqa: E402

BLOCK = "Dossier: 2026-001 - T c. L\nType: Audience"


@pytest.mark.parametrize("text, blank, expected", [
    ("", False, ""),
    ("Note", False, "Note"),
    (BLOCK, False, ""),
    (f"Note\n{BLOCK}", False, "Note"),
    (f"Note\n{BLOCK}\nAjout", False, "Note\nAjout"),
    (f"{BLOCK}\n{BLOCK}\n{BLOCK}", False, ""),
    ("Note\r\n" + BLOCK.replace("\n", "\r\n"), False, "Note"),
    ("Note\r\n" + BLOCK.replace("\n", "\r\n") + "\r\nAjout", False,
     "Note\r\nAjout"),
    # A partial or retouched run is the lawyer's.
    ("Dossier: 2026-001 - T c. L", False, "Dossier: 2026-001 - T c. L"),
    (f"Note\n{BLOCK}!", False, f"Note\n{BLOCK}!"),
    (f"x{BLOCK}", False, f"x{BLOCK}"),
    # The blank-line separator (tasks) goes with its line.
    ("Desc\n\nDossier: 2026-001 - T c. L\nType: Audience", True, "Desc"),
    # Other line endings are returned byte for byte.
    ("Note\u2028suite", False, "Note\u2028suite"),
])
def test_remove_served_block(text, blank, expected):
    assert remove_served_block(text, BLOCK, blank_line_before=blank) == expected


def test_an_empty_block_removes_nothing():
    assert remove_served_block("Note\n", "", blank_line_before=True) == "Note\n"


def test_it_is_linear_by_construction():
    """No regex anywhere in the module (the CWE-1333 doctrine of the fill
    engine): a long text is one pass over its lines."""
    import inspect
    assert "import re" not in inspect.getsource(dav_text)
    long = ("x\n" * 200_000) + BLOCK
    assert remove_served_block(long, BLOCK, blank_line_before=False) == \
        ("x\n" * 200_000)[:-1]
