"""The two Markup builders Bandit's B704 used to flag: ``utils.html_attr.jsattr``
(the Jinja filter of the Alpine ``x-data`` blocks) and ``utils.icons.ms``.

Both now build their Markup through markupsafe's own escaper — ``escape()``
for the filter, ``Markup(literal).format(...)`` for the icon — so the XSS
guarantee is the library's, and these tests pin what it has to deliver:
the value the browser hands Alpine (the attribute AFTER entity decoding) is
exactly the JSON of the input, and the icon markup is byte-identical to what
the hand-written escaping produced.
"""

from __future__ import annotations

import html
import json
import os
import sys
from html.parser import HTMLParser

import pytest
from jinja2 import Environment
from markupsafe import Markup, escape

ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ATHENA_DIR not in sys.path:
    sys.path.insert(0, ATHENA_DIR)

from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402

HOSTILE = [
    "Succession de L'Heureux",
    'un « " » au milieu',
    "x');alert(1);('",
    '"; alert(1); "',
    "</div><script>alert(1)</script>",
    "&amp; déjà une entité",
    "a\\b",
    "ligne\nsuivante",
    "séparateur de ligne",
    "",
    None,
    42,
]


class _Attrs(HTMLParser):
    """The browser's side: entities decoded, attribute by attribute."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attrs: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        self.attrs.update({k: v for k, v in attrs if v is not None})


def _render(value) -> str:
    env = Environment(autoescape=True)
    env.filters["jsattr"] = jsattr
    return env.from_string(
        '<div x-data="{ v: {{ value|jsattr }} }"></div>'
    ).render(value=value)


@pytest.mark.parametrize("value", HOSTILE)
def test_alpine_reads_exactly_the_json_of_the_value(value):
    parser = _Attrs()
    parser.feed(_render(value))
    expression = parser.attrs["x-data"]
    assert expression.startswith("{ v: ") and expression.endswith(" }")
    literal = expression[len("{ v: "):-len(" }")]
    assert json.loads(literal) == str(value)


@pytest.mark.parametrize("value", HOSTILE)
def test_nothing_can_leave_the_attribute(value):
    out = str(jsattr(value))
    # Every character that could end the attribute or open a tag travels
    # as an entity; every « & » starts one.
    assert not set(out) & set("\"'<>")
    assert html.unescape(out) == json.dumps(str(value), ensure_ascii=False)


def test_the_filter_returns_markup_so_autoescape_never_escapes_twice():
    assert isinstance(jsattr("x"), Markup)
    rendered = _render("L'Heureux & fils")
    assert "&amp;amp;" not in rendered and "&amp;#" not in rendered
    assert "&#34;L&#39;Heureux &amp; fils&#34;" in rendered


def test_main_registers_this_filter_and_no_copy():
    source = open(os.path.join(ATHENA_DIR, "main.py"), encoding="utf-8").read()
    assert "from utils.html_attr import jsattr" in source
    assert 'filters["jsattr"] = jsattr' in source
    assert "def _jsattr" not in source


def _old_ms(name, size=20, classes="", fill=False):
    """The pre-2026-09-30 body of utils.icons.ms, verbatim — the reference
    the Markup.format rewrite must reproduce byte for byte."""
    cls = f"ms ms-{size}"
    if fill:
        cls += " ms-fill"
    if classes:
        cls += f" {classes}"
    return (f'<span class="{escape(cls)}" aria-hidden="true" '
            f'translate="no">{escape(name)}</span>')


@pytest.mark.parametrize("kwargs", [
    {"name": "delete"},
    {"name": "delete", "size": 16, "classes": "text-red-600"},
    {"name": "bookmark", "fill": True},
    {"name": "settings", "classes": 'a" onmouseover="alert(1)'},
    {"name": "delete", "classes": "<b>&'"},
])
def test_ms_is_byte_identical_to_the_hand_escaped_version(kwargs):
    out = ms(**kwargs)
    assert isinstance(out, Markup)
    assert str(out) == _old_ms(**kwargs)
