"""Le composant de pagination — la barre partagée par les 7 listes.

Un seul gabarit sert les deux modes et les sept vues, donc un défaut ici est un
défaut partout. Ces épingles couvrent les trois façons dont il casse EN
SILENCE : une classe absente de l'artefact compilé (elle ne s'applique pas,
sans erreur), un contrôle qui oublie `page` (le libellé décroche des données),
et un contrôle qui oublie `hx-include` (les filtres actifs disparaissent, donc
la liste échangée n'est plus celle qu'on regardait).
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pagination import (  # noqa: E402
    JUMP_PAGES,
    PAGE_SIZE,
    cursor_pagination,
    paginate,
)

# Assez de pages pour qu'un ±JUMP_PAGES atterrisse STRICTEMENT entre Début et
# Fin dans les deux sens — c'est le seul état où les quatre sauts coexistent.
_GROS_PAGES = 2 * JUMP_PAGES + 4

_RACINE = Path(__file__).resolve().parent.parent
_GABARIT = "components/pagination.html"

_CLASS_RE = re.compile(r'class="([^"]+)"')
_BUTTON_RE = re.compile(r"<button\b.*?</button>", re.S)
_MS_CALL_RE = re.compile(r"""\bms\(\s*['"]([a-z0-9_]+)['"]""")
_ARIA_RE = re.compile(r'aria-label="[^"]+"')


def _env():
    from jinja2 import Environment, FileSystemLoader

    from utils.icons import ms

    e = Environment(loader=FileSystemLoader(str(_RACINE / "templates")))
    # Le VRAI ms() : il émet les classes .ms/.ms-16, qui doivent elles aussi
    # exister dans l'artefact, et il lève sur un nom hors du sous-ensemble.
    e.globals.update(ms=ms, url_for=lambda *a, **k: "#")
    return e


def _rendu(ctx):
    return _env().get_template(_GABARIT).render(pagination=ctx)


def _page_ctx(total, page, **extra):
    _, ctx = paginate(list(range(total)), page)
    ctx.update(url="/liste", target="#rows", **extra)
    return ctx


def _cursor_ctx(page, total, *, next_cursor="c9", trail=None, cursor="c1", **extra):
    return cursor_pagination(
        cursor=cursor,
        trail=["c0"] if trail is None else trail,
        next_cursor=next_cursor,
        url="/liste",
        target="#rows",
        page=page,
        total=total,
        **extra,
    )


def _matrice():
    """Tous les branchements du gabarit, pas un seul état heureux.

    Une classe employée uniquement dans une branche rare passerait sinon
    inaperçue — et une classe absente ne se voit pas, elle n'agit pas.
    """
    gros = _GROS_PAGES * PAGE_SIZE      # assez de pages pour les ±N
    petit = 3 * PAGE_SIZE               # trop peu : ni ±N, ni Fin partout
    cas = []
    for total in (petit, gros):
        pages = -(-total // PAGE_SIZE)
        for page in (1, 2, 3, max(1, pages - 1), pages):
            cas.append(("page/%d/%d" % (total, page), _page_ctx(total, page)))
    for total in (None, petit, gros):
        for page in (1, 2, 3, 40):
            cas.append(("cur/%s/%d" % (total, page), _cursor_ctx(page, total)))
            cas.append(("fin/%s/%d" % (total, page),
                        _cursor_ctx(page, total, next_cursor=None)))
    # Vraie première page en mode curseur : aucun curseur, aucune traîne.
    cas.append(("cur/p1", _cursor_ctx(1, gros, cursor=None, trail=[])))
    # Avec extra_vals — le cas des onglets de /temps.
    cas.append(("cur/extra", _cursor_ctx(5, gros, extra_vals={"tab": "depenses"})))
    return cas


def _classes(html):
    out = set()
    for bloc in _CLASS_RE.findall(html):
        out.update(c for c in bloc.split() if c and not c.startswith("{"))
    return out


def _echappe(c):
    b = chr(92)
    for brut, ech in ((b, b * 2), (":", b + ":"), (".", b + "."), ("/", b + "/"),
                      ("[", b + "["), ("]", b + "]")):
        c = c.replace(brut, ech)
    return c


# ── L'artefact compilé ─────────────────────────────────────────────────────

def test_every_class_in_every_branch_exists_in_the_compiled_artifact():
    """La promesse « aucune recompilation » doit rester vraie au prochain lot.

    L'artefact est verrouillé dans SIX fichiers plus une assertion littérale
    (`test_security_headers.py`), donc une classe neuve n'est pas une retouche
    de gabarit : c'est un éventail de sept fichiers. Le glob est insensible au
    hachage, si bien que ce test survit à une recompilation légitime.
    """
    css = next(_RACINE.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    manquantes = {}
    for nom, ctx in _matrice():
        for c in sorted(_classes(_rendu(ctx))):
            if ("." + _echappe(c)) not in css:
                manquantes.setdefault(c, nom)
    assert not manquantes, "classes absentes de l'artefact : %r" % (manquantes,)


def test_the_bar_uses_exactly_the_six_navigation_glyphs():
    """La barre est ICON-ONLY depuis le 2026-09-11 : six glyphes, zéro libellé.

    `tests/test_icons.py` épingle le sous-ensemble DANS LES DEUX SENS, donc
    ajouter un nom ici sans régénérer la police woff2 laisse la suite VERTE
    et rend le mot littéral à l'écran (le piège M19). Ces six noms ont été
    régénérés ensemble — v369 → v371, 41 → 45 glyphes.
    """
    src = (_RACINE / "templates" / _GABARIT).read_text(encoding="utf-8")
    assert set(_MS_CALL_RE.findall(src)) == {
        "chevron_left", "chevron_right",
        "keyboard_double_arrow_left", "keyboard_double_arrow_right",
        "first_page", "last_page",
    }


def test_every_glyph_the_bar_names_is_governed_by_the_icon_set():
    """Le nom passé à ms() doit vivre dans MATERIAL_ICONS.

    ms() lève sur un nom inconnu, donc le rendu l'attraperait — mais il ne
    l'attraperait que dans la branche qui l'emploie, et « Première page » ne
    paraît qu'à partir de la page 3.
    """
    from utils.icons import MATERIAL_ICONS
    src = (_RACINE / "templates" / _GABARIT).read_text(encoding="utf-8")
    assert set(_MS_CALL_RE.findall(src)) <= set(MATERIAL_ICONS)


# ── Le contrat des hx-vals ─────────────────────────────────────────────────

def test_every_control_emits_the_page_param():
    """Le défaut qui décroche le libellé des données.

    Si seul le groupe de saut portait `page`, après un saut le libellé
    retomberait à 3/4/5 pendant que les données liraient 13/14/15, « +10 »
    deviendrait un bouton mort, et le gel à 22 resterait entier.
    """
    for nom, ctx in _matrice():
        for ctrl in _BUTTON_RE.findall(_rendu(ctx)):
            assert '"page"' in ctrl, (nom, ctrl[:120])


def test_every_control_carries_hx_include_and_extra_vals():
    """Sans hx-include, un saut échange des heures sous un titre « Dépenses »."""
    html = _rendu(_cursor_ctx(JUMP_PAGES + 2, _GROS_PAGES * PAGE_SIZE,
                              extra_vals={"tab": "depenses"}))
    ctrls = _BUTTON_RE.findall(html)
    assert len(ctrls) >= 5, "cet état devrait offrir la marche ET le saut"
    for ctrl in ctrls:
        assert 'hx-include="#filters input, #filters select"' in ctrl
        assert '"tab": "depenses"' in ctrl


def test_leap_controls_clear_the_cursor():
    """Vider le curseur EST ce qui aiguille la requête vers la page absolue.

    Le filtre porte sur l'`aria-label`, jamais sur un libellé visible : la
    barre est icon-only, et un contrôle sans nom accessible serait de toute
    façon un défaut que `test_every_control_is_a_button_with_an_aria_label`
    fait tomber.
    """
    html = _rendu(_cursor_ctx(JUMP_PAGES + 2, _GROS_PAGES * PAGE_SIZE))
    sauts = [c for c in _BUTTON_RE.findall(html)
             if "page (" in c or "Première page" in c or "de %d pages" % JUMP_PAGES in c]
    assert len(sauts) == 4, sauts
    for ctrl in sauts:
        assert '"cursor": ""' in ctrl and '"trail": ""' in ctrl


# ── Les gardes d'affichage ─────────────────────────────────────────────────

def test_the_bar_is_hidden_for_a_single_page():
    """Les clés ajoutées ne doivent pas avoir desserré la garde extérieure."""
    assert _rendu(_page_ctx(PAGE_SIZE - 1, 1)).strip() == ""
    assert _rendu(None).strip() == ""


def test_a_missing_total_hides_the_leap_row_entirely():
    """Compte illisible → la barre est visuellement celle d'avant ce lot."""
    html = _rendu(_cursor_ctx(7, None))
    assert "Dernière page" not in html
    assert "de %d pages" % JUMP_PAGES not in html
    # La marche reste offerte — elle ne dépend d'aucun total.
    assert "Page précédente" in html and "Page suivante" in html
    indicateur = " ".join(html.split())
    assert "Page 7 <" in indicateur or "Page 7</span>" in indicateur


def test_the_total_is_shown_when_it_is_known():
    assert "Page 2 / 7" in " ".join(_rendu(_page_ctx(7 * PAGE_SIZE, 2)).split())


def test_every_control_is_a_button_with_an_aria_label():
    """Les <a> sans href n'étaient ni focusables, ni au clavier, ni annoncés.

    ms() rend `aria-hidden`, donc le glyphe ne contribue rien au nom
    accessible : chaque contrôle porte le sien.
    """
    for nom, ctx in _matrice():
        html = _rendu(ctx)
        assert "<a " not in html, nom
        for ctrl in _BUTTON_RE.findall(html):
            assert _ARIA_RE.search(ctrl), (nom, ctrl[:120])
