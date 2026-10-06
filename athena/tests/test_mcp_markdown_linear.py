"""The connector's Markdown normalization is LINEAR (finitions, robustness-2).

``mcp.handlers._normalize_markdown`` rewrote bare-address autolinks with
``<([^<>\\s@]+@[^<>\\s@]+\\.[^<>\\s@]+)>`` — QUADRATIC: the class after « @ »
also matches « . », so ``<a@`` + ``b.``×n with no closing « > » tried every
split point and rescanned the tail (the ``normalize_email`` ReDoS fixed in
``utils/validators.py``). Harmless at the 20 000 characters its callers were capped at;
``update_note`` / ``edit_analyse`` (99 000) and ``create_document``
(60 000) route far longer text through it — measured 20,65 s for 99 000
characters, the GIL held, every thread of the worker frozen.

The address autolink is now a linear scan, proven here to recognise the
IDENTICAL language (against the retired regex, over a seeded fuzz corpus),
and every pattern left in ``mcp/handlers.py`` passes a structural linearity
tripwire the docx modules' one did not cover.
"""

import os
import random
import re
import sys
import time
from unittest import mock

import pytest

try:  # Python 3.11+: the parser moved under re.
    import re._parser as sre_parse
    import re._constants as sre_constants
except ImportError:  # pragma: no cover
    import sre_constants
    import sre_parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers

# The pattern the scanner replaced — kept HERE only, as the oracle.
_RETIRED = re.compile(r"<([^<>\s@]+@[^<>\s@]+\.[^<>\s@]+)>")


def _retired_sub(text: str) -> str:
    return _RETIRED.sub(lambda m: f"[{m.group(1)}](mailto:{m.group(1)})", text)


@pytest.mark.parametrize("text", [
    "", "rien", "<jean@exemple.ca>", "Écrire à <jean@exemple.ca> demain.",
    "<a@b.c><d@e.f>", "<<a@b.c>", "<a@b.c>>", "<a@@b.c>", "<@b.c>",
    "<a@.b>", "<a@b.>", "<a@b>", "<a b@c.d>", "<a@b.c >", "<a@b.c",
    "a@b.c", "<a@b.c.d.e>", "<x<a@b.c>", "<https://x.y>", "<a@b.c>\n<d@e.f>",
])
def test_the_scanner_agrees_with_the_retired_regex(text):
    assert handlers._email_autolinks(text) == _retired_sub(text)


def test_the_scanner_agrees_on_a_seeded_fuzz_corpus():
    rng = random.Random(20260929)
    alphabet = "<>@.ab \n é "
    for _ in range(20000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        assert handlers._email_autolinks(text) == _retired_sub(text), repr(text)


@pytest.mark.parametrize("hostile", [
    "<a@" + "b." * 49498,                       # the measured 20,65 s case
    "<" * 99000,                                # candidates, no « > »
    "<" * 98999 + ">",                          # one « > » far away
    ("<a@b." + "c" * 10) * 6000,                # many unclosed candidates
], ids=["dotted-tail", "no-close", "far-close", "unclosed-many"])
def test_a_hostile_99k_body_normalizes_in_linear_time(hostile):
    started = time.perf_counter()
    handlers._normalize_markdown(hostile)
    assert time.perf_counter() - started < 1.0


def test_the_normalization_still_rewrites_every_autolink_kind():
    text = "Voir <https://canlii.ca/t/abc>, <mailto:a@b.c> et <jean@exemple.ca>."
    assert handlers._normalize_markdown(text) == (
        "Voir [https://canlii.ca/t/abc](https://canlii.ca/t/abc), "
        "[a@b.c](mailto:a@b.c) et [jean@exemple.ca](mailto:jean@exemple.ca).")


# ── the structural tripwire ──────────────────────────────────────────────

_REPEATS = {sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT}
_POSSESSIVE = getattr(sre_constants, "POSSESSIVE_REPEAT", None)
if _POSSESSIVE is not None:
    _REPEATS.add(_POSSESSIVE)


def _single_class(body) -> list | None:
    """The one-character matcher a repeat's body is, or None."""
    items = list(body)
    if len(items) != 1:
        return None
    op, av = items[0]
    if op in (sre_constants.IN, sre_constants.LITERAL,
              sre_constants.NOT_LITERAL, sre_constants.ANY):
        return items
    return None


def _matches(matcher, ch: str) -> bool:
    """Whether a one-character sre matcher accepts *ch* — evaluated by the
    engine itself on a pattern rebuilt from the parsed item."""
    op, av = matcher[0]
    if op is sre_constants.ANY:
        return ch != "\n"
    if op is sre_constants.LITERAL:
        return ord(ch) == av
    if op is sre_constants.NOT_LITERAL:
        return ord(ch) != av
    negate = False
    hit = False
    for sub_op, sub_av in av:
        if sub_op is sre_constants.NEGATE:
            negate = True
        elif sub_op is sre_constants.LITERAL:
            hit |= ord(ch) == sub_av
        elif sub_op is sre_constants.RANGE:
            hit |= sub_av[0] <= ord(ch) <= sub_av[1]
        elif sub_op is sre_constants.CATEGORY:
            probe = {
                sre_constants.CATEGORY_SPACE: r"\s",
                sre_constants.CATEGORY_NOT_SPACE: r"\S",
                sre_constants.CATEGORY_DIGIT: r"\d",
                sre_constants.CATEGORY_NOT_DIGIT: r"\D",
                sre_constants.CATEGORY_WORD: r"\w",
                sre_constants.CATEGORY_NOT_WORD: r"\W",
            }.get(sub_av)
            hit |= bool(probe and re.fullmatch(probe, ch))
    return hit != negate


def _children(op, av):
    if op in _REPEATS:
        return [av[2]]
    if op is sre_constants.SUBPATTERN:
        return [av[3]]
    if op is sre_constants.BRANCH:
        return list(av[1])
    return []


def overlapping_split(items) -> bool:
    """A repeat of a class, then a literal that class ALSO matches, then
    another repeat: ``[^@.]+`` style ``x+\\.x+`` — every split point of the
    run is tried when the match fails later, the quadratic shape."""
    items = list(items)
    for index in range(len(items) - 2):
        (op1, av1), (op2, av2), (op3, _av3) = items[index:index + 3]
        if (op1 in _REPEATS and av1[1] > 1 and op2 is sre_constants.LITERAL
                and op3 in _REPEATS):
            matcher = _single_class(av1[2])
            if matcher is not None and _matches(matcher, chr(av2)):
                return True
    return any(overlapping_split(c) for op, av in items for c in _children(op, av))


def test_the_tripwire_catches_the_retired_pattern_and_spares_linear_ones():
    assert overlapping_split(sre_parse.parse(_RETIRED.pattern))
    assert overlapping_split(sre_parse.parse(r"[^@]+\.[^@]+"))
    assert not overlapping_split(sre_parse.parse(r"[^<>\s@.]+(?:\.[^<>\s@.]+)+"))
    assert not overlapping_split(sre_parse.parse(r"<((?:https?|ftp)://[^<>\s]+)>"))


def test_every_pattern_of_the_handlers_is_linear():
    patterns = [v for v in vars(handlers).values() if isinstance(v, re.Pattern)]
    assert patterns, "the sweep found no pattern"
    for pattern in patterns:
        assert not pattern.flags & re.DOTALL, pattern.pattern
        assert not overlapping_split(sre_parse.parse(pattern.pattern)), (
            pattern.pattern)
