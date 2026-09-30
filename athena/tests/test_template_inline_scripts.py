"""Every inline script of every template stays parseable as JavaScript.

CodeQL's JavaScript extractor reads the `<script>` blocks of our Jinja
templates as JavaScript (js/syntax-error, 2026-09-30). A placeholder in CODE
position — `const cfg = {{ x|tojson }};` — is understood; the two shapes it
could not parse, found by its analysis of `main` @ 319a064, are pinned here so
they do not come back:

1. **A Jinja expression inside a JS string literal holding that string's own
   quote.** `'{{ url_for('documents.api_finaliser') }}'` renders perfectly,
   but read as JavaScript the second `'` closes the string and
   `documents.api_finaliser` is a stray token — so CodeQL analysed NOTHING in
   upload.html's script (and never would have reported a real defect there).
   Use the other quote inside the expression: `'{{ url_for("x") }}'`.
2. **`<script` inside a Jinja comment.** Jinja drops `{# … #}` at render, but
   the extractor sees the raw file: `died on its own <script src> line` in a
   base.html comment opened a phantom script whose « body » was the rest of
   the comment's prose.

Plus one conservative rule, because nothing proves how the extractor reads it
and no template uses it today: no Jinja STATEMENT (`{% … %}`) inside an
inline executable script — compute in the route, or emit a data block
(`<script type="application/json">`, which is never executed and is skipped
here, as are `src=` scripts).

The lexer below tracks strings and comments only; a quote inside a regex
literal would mislead it — should one ever appear, the failure names the line.
"""

import pathlib
from html.parser import HTMLParser

import pytest

ATHENA = pathlib.Path(__file__).resolve().parent.parent
_TEMPLATE_ROOTS = (ATHENA / "templates", ATHENA / "client" / "templates")
_EXECUTABLE_TYPES = ("", "text/javascript", "application/javascript", "module")


def _without_jinja_comments(text: str) -> tuple[str, list[tuple[int, str]]]:
    """The text with every `{# … #}` blanked (newlines kept, so line numbers
    hold), and the comments themselves with their first line."""
    kept, comments, i = [], [], 0
    while True:
        start = text.find("{#", i)
        end = text.find("#}", start + 2) if start >= 0 else -1
        if start < 0 or end < 0:
            kept.append(text[i:])
            break
        kept.append(text[i:start])
        kept.append("\n" * text.count("\n", start, end + 2))
        comments.append((text.count("\n", 0, start) + 1, text[start:end + 2]))
        i = end + 2
    return "".join(kept), comments


class _InlineScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[tuple[int, str]] = []
        self._open: "tuple[int, list[str]] | None" = None

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        attributes = dict(attrs)
        kind = (attributes.get("type") or "").strip().lower()
        if "src" in attributes or kind not in _EXECUTABLE_TYPES:
            self._open = None
            return
        self._open = (self.getpos()[0], [])

    def handle_data(self, data):
        if self._open is not None:
            self._open[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._open is not None:
            self.blocks.append((self._open[0], "".join(self._open[1])))
            self._open = None


def _jinja_in_script(js: str):
    """Yield (offset, delimiter, JS state, Jinja text) for each `{{`/`{%` —
    the state is `code`, or the quote of the string it sits in."""
    i, state, n = 0, "code", len(js)
    while i < n:
        for opener, closer in (("{{", "}}"), ("{%", "%}")):
            if js.startswith(opener, i):
                end = js.find(closer, i + 2)
                end = n if end < 0 else end + 2
                yield i, opener, state, js[i:end]
                i = end
                break
        else:
            char = js[i]
            if state == "code":
                if js.startswith("//", i):
                    nl = js.find("\n", i)
                    i = n if nl < 0 else nl
                    continue
                if js.startswith("/*", i):
                    close = js.find("*/", i + 2)
                    i = n if close < 0 else close + 2
                    continue
                if char in "'\"`":
                    state = char
            elif char == "\\":
                i += 2
                continue
            elif char == state:
                state = "code"
            i += 1


def template_script_violations(text: str, name: str) -> list[str]:
    body, comments = _without_jinja_comments(text)
    found = [
        f"{name}:{line}: « <script » inside a Jinja comment"
        for line, comment in comments if "<script" in comment.lower()
    ]
    parser = _InlineScripts()
    parser.feed(body)
    parser.close()
    for first_line, js in parser.blocks:
        for offset, opener, state, jinja in _jinja_in_script(js):
            line = first_line + js.count("\n", 0, offset)
            if opener == "{%":
                found.append(f"{name}:{line}: Jinja statement in an inline script")
            elif state != "code" and state in jinja[2:-2]:
                found.append(
                    f"{name}:{line}: {state} inside a Jinja expression that "
                    f"sits in a {state}-quoted JS string"
                )
    return found


def _templates():
    for root in _TEMPLATE_ROOTS:
        yield from sorted(root.rglob("*.html"))


def test_every_inline_script_stays_parseable():
    offenders, scanned = [], 0
    for path in _templates():
        scanned += 1
        offenders += template_script_violations(
            path.read_text(encoding="utf-8"), path.relative_to(ATHENA).as_posix())
    assert scanned > 100, "the sweep did not walk the templates"
    assert offenders == []


def test_the_sweep_reads_the_scripts_it_exists_for():
    """Not vacuous: it finds the executable blocks, and the placeholders in
    them, of the two files CodeQL could not parse."""
    for rel in ("templates/documents/upload.html", "templates/base.html"):
        body, _ = _without_jinja_comments(
            (ATHENA / rel).read_text(encoding="utf-8"))
        parser = _InlineScripts()
        parser.feed(body)
        assert parser.blocks, rel
    upload = (ATHENA / "templates/documents/upload.html").read_text(encoding="utf-8")
    body, _ = _without_jinja_comments(upload)
    parser = _InlineScripts()
    parser.feed(body)
    states = {state for _l, js in parser.blocks
              for _o, _d, state, _j in _jinja_in_script(js)}
    assert "'" in states


@pytest.mark.parametrize("snippet, flagged", [
    ("<script>let u = '{{ url_for('x') }}';</script>", True),
    ('<script>let u = "{{ url_for("x") }}";</script>', True),
    ("<script>let u = '{{ url_for(\"x\") }}';</script>", False),
    ("<script>const c = {{ cfg|tojson }};</script>", False),
    ("<script>// '{{ a('b') }}'\nlet x = 1;</script>", False),
    ("<script>{% if x %}let a = 1;{% endif %}</script>", True),
    ("<script type=\"application/json\">{{ data|tojson }} '{{ f('x') }}'</script>", False),
    ("<script src=\"/s.js\"></script>{# the <script src> tag #}", True),
    ("{# a script tag, said without its bracket #}", False),
    ("<p>'{{ url_for('x') }}'</p>", False),
])
def test_the_sweep_catches_what_it_claims(snippet, flagged):
    assert bool(template_script_violations(snippet, "snippet")) is flagged
