"""Material Symbols Outlined — canonical icon subset + the ``ms`` Jinja global.

The vendored woff2 in ``static/vendor/`` contains EXACTLY the ligature names
in :data:`MATERIAL_ICONS` — the single place of governance for the icon set.
``tests/test_icons.py`` pins template usage == this set (both directions:
a name used but not vendored would render as raw text; a name vendored but
never used is dead weight).

Adding an icon (full procedure in ``utils/fonts/README.md``):
1. add the name here; 2. rebuild the css2 URL (sorted ``icon_names=``) and
re-download the subset with a browser User-Agent; 3. sha256 → NEW file name
``material-symbols-outlined-vNNN-<sha8>.woff2`` (vendored assets are
immutable — never edit in place) + delete the old one; 4. update the
``url()`` in ``app.input.css`` → recompile + rehash the CSS → full asset
fan-out; 5. update README (URL/sha256); 6. run the suite.
"""

from markupsafe import Markup, escape

MATERIAL_ICONS: frozenset = frozenset({
    "add", "archive", "arrow_back", "assignment", "bookmark",
    "calendar_month", "call", "check", "check_circle", "chevron_left",
    "chevron_right", "close", "delete", "description", "download", "draft",
    "edit", "error", "folder_open", "forum", "grid_view", "group", "image",
    "info",
    "inventory_2", "list", "lock", "logout", "mail", "more_horiz",
    "more_vert", "payments", "person", "picture_as_pdf", "print",
    "push_pin", "schedule", "smartphone", "undo", "upload", "warning",
})

# Every size must have a hand-written .ms-N rule in app.input.css.
_SIZES = frozenset({10, 12, 14, 16, 20, 24, 28, 40, 48, 64})


def ms(name: str, size: int = 20, classes: str = "", fill: bool = False) -> Markup:
    """Render a Material Symbols glyph (ligature) as an inline ``<span>``.

    ``aria-hidden`` is unconditional: a ligature exposes its text
    ("delete") to screen readers — the ACCESSIBLE label always lives on
    the parent (``title`` + ``aria-label`` on the button/link, the app's
    existing pattern). ``translate="no"`` guards the ligature text against
    DOM-rewriting translators (belt-and-braces beside the main app's
    global ``translate="no"``).
    """
    if name not in MATERIAL_ICONS:
        raise ValueError(
            f"unknown icon {name!r} — not in the vendored subset "
            "(see utils/icons.py MATERIAL_ICONS)"
        )
    if size not in _SIZES:
        raise ValueError(
            f"no .ms-{size} size class exists (add it to app.input.css)"
        )
    cls = f"ms ms-{size}"
    if fill:
        cls += " ms-fill"
    if classes:
        cls += f" {classes}"
    return Markup(
        f'<span class="{escape(cls)}" aria-hidden="true" '
        f'translate="no">{escape(name)}</span>'
    )
