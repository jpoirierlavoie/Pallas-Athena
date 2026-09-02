"""Tests for utils/icons.py — Material Symbols governance (anti-drift).

These tests keep templates and MATERIAL_ICONS in agreement in BOTH
directions: a name used but not vendored would render as raw text on
screen; a name vendored but never used is dead weight.

The woff2 is a SUPERSET since 2026-09-02: dropping the chat took `forum`
out of the vocabulary, and the glyph stays in the file until the next
regeneration — ~600 bytes of 24 Ko, against a full asset fan-out (six
files plus a pinned Early-Hints literal) for no functional gain. Nothing
here reads the font's glyph table, so the agreement these tests enforce
is between templates and the Python set.
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.icons import MATERIAL_ICONS, ms

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
VENDOR = Path(__file__).resolve().parents[1] / "static" / "vendor"
CALL_RE = re.compile(r"""\bms\(\s*['"]([a-z0-9_]+)['"]""")


def _used() -> set:
    used = set()
    for f in TEMPLATES.rglob("*.html"):
        used |= set(CALL_RE.findall(f.read_text(encoding="utf-8")))
    return used


def test_every_icon_used_is_in_the_vendored_subset():
    missing = _used() - MATERIAL_ICONS
    assert not missing, (
        f"glyphes appelés mais absents du woff2 vendu : {sorted(missing)} — "
        "ils s'afficheraient comme texte brut (procédure d'ajout : "
        "utils/icons.py + utils/fonts/README.md)"
    )


def test_subset_carries_no_dead_glyphs():
    dead = MATERIAL_ICONS - _used()
    assert not dead, f"glyphes vendus jamais appelés : {sorted(dead)}"


def test_no_stray_inline_heroicon_svg():
    # Seuls les spinners restent en SVG (décision : arcs animés ≠ glyphes,
    # et le spinner de login doit exister AVANT que la police charge).
    allowed = {
        "auth/login.html", "auth/mfa_manage.html",
        "auth/mfa_setup.html", "documents/upload.html",
    }
    for f in TEMPLATES.rglob("*.html"):
        rel = f.relative_to(TEMPLATES).as_posix()
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"<svg\b[^>]*>", src):
            tag = m.group(0)
            assert rel in allowed and (
                "spinner" in tag or "animate-spin" in tag
            ), f"SVG inline résiduel dans {rel}: {tag[:80]}"


def test_vendored_font_file_exists_and_matches_css():
    fonts = list(VENDOR.glob("material-symbols-outlined-*.woff2"))
    assert len(fonts) == 1, fonts
    css = (Path(__file__).resolve().parents[1] / "static" / "src"
           / "app.input.css").read_text(encoding="utf-8")
    assert fonts[0].name in css, (
        f"le @font-face de app.input.css ne référence pas {fonts[0].name}"
    )
    # Licence sœur présente (Apache-2.0, pas OFL).
    assert list(VENDOR.glob("material-symbols-outlined-*Apache-2.0.txt"))


def test_ms_rejects_unknown_name_and_size():
    with pytest.raises(ValueError):
        ms("icone_inexistante")
    with pytest.raises(ValueError):
        ms("delete", size=17)


def test_ms_emission_shape():
    html = str(ms("delete", 20, "text-red-600"))
    assert 'aria-hidden="true"' in html
    assert 'translate="no"' in html
    assert 'class="ms ms-20 text-red-600"' in html
    assert ">delete</span>" in html
    filled = str(ms("bookmark", 20, fill=True))
    assert "ms-fill" in filled


def test_every_size_class_exists_in_css():
    from utils.icons import _SIZES

    css = (Path(__file__).resolve().parents[1] / "static" / "src"
           / "app.input.css").read_text(encoding="utf-8")
    for size in sorted(_SIZES):
        assert f".ms-{size} " in css or f".ms-{size}{{" in css.replace(" ", ""), (
            f".ms-{size} absent de app.input.css"
        )
