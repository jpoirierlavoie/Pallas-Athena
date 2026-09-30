"""The ``jsattr`` Jinja filter — a value as a JavaScript string literal
inside a double-quoted HTML attribute.

Its callers are the Alpine ``x-data="{ … }"`` blocks of the edit forms,
where a raw interpolation of a title carrying an apostrophe (« L'Heureux »)
ended the JS string and broke the whole component, and an echoed hostile
value could inject an expression (``'unsafe-eval'`` is on for Alpine).

Two encodings, in this order, each by its library: ``json.dumps`` makes the
value a JS string literal, then ``markupsafe.escape`` makes that literal
safe inside the attribute (``& < > " '`` become entities, which the browser
decodes BEFORE Alpine reads the attribute — so the expression Alpine
evaluates is exactly the JSON). The result is ``Markup``, which autoescape
passes through unchanged instead of escaping a second time.

Pure (json + markupsafe): ``main.create_app`` registers it, and a test that
asserts on ``x-data`` output registers THIS function — never a copy, and
never the ``lambda v: v`` stub other harnesses use, under which autoescape
renders the value as escaped text rather than a JS literal.
"""

from __future__ import annotations

import json

from markupsafe import Markup, escape


def jsattr(value: object) -> Markup:
    """*value* (as ``str``) as a JS string literal, escaped for a
    double-quoted HTML attribute."""
    return escape(json.dumps(str(value), ensure_ascii=False))
