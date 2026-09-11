"""Le composant de pagination — la barre partagée par les 7 listes.

Un seul gabarit sert les deux modes et les sept vues, donc un défaut ici est un
défaut partout. Ces épingles couvrent les quatre façons dont il casse EN
SILENCE : une classe absente de l'artefact compilé (elle ne s'applique pas,
sans erreur), un contrôle qui oublie `page` (le libellé décroche des données),
un contrôle qui oublie `hx-include` (les filtres actifs disparaissent, donc la
liste échangée n'est plus celle qu'on regardait), et — depuis le 2026-09-11 —
un contrôle GRISÉ qui garde son câblage htmx ou perd son nom.
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

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
# L'ATTRIBUT nu, jamais la sous-chaîne. La chaîne de classes porte
# `disabled:opacity-60`, donc « disabled » est présent sur les boutons VIVANTS
# aussi : un `if "disabled" in ctrl: continue` rendrait vert-à-vide le contrat
# des hx-vals ci-dessous — exactement la classe de panne silencieuse que ce
# fichier existe pour attraper.
_DISABLED_RE = re.compile(r"\sdisabled(?=[\s>/])")


def _actifs(html):
    return [c for c in _BUTTON_RE.findall(html) if not _DISABLED_RE.search(c)]


def _inertes(html):
    return [c for c in _BUTTON_RE.findall(html) if _DISABLED_RE.search(c)]


def _empreinte(html):
    """La suite ORDONNÉE des noms accessibles — compte ET ordre en une valeur."""
    return tuple(_ARIA_RE.search(b).group(0) for b in _BUTTON_RE.findall(html))


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
    l'attraperait que dans la branche qui l'emploie, et « Fin » ne paraît pas
    du tout sur une liste dont le compte a échoué.
    """
    from utils.icons import MATERIAL_ICONS
    src = (_RACINE / "templates" / _GABARIT).read_text(encoding="utf-8")
    assert set(_MS_CALL_RE.findall(src)) <= set(MATERIAL_ICONS)


# ── Le contrat des hx-vals ─────────────────────────────────────────────────

def test_a_control_carries_the_whole_hx_contract_or_none_of_it():
    """Le défaut qui décroche le libellé des données, et son revers.

    Si seul le groupe de saut portait `page`, après un saut le libellé
    retomberait à 3/4/5 pendant que les données liraient 13/14/15, « +10 »
    deviendrait un bouton mort, et le gel à 22 resterait entier. Depuis que
    les contrôles inapplicables sont GRISÉS plutôt que retirés, la règle a un
    revers : un bouton inerte ne porte AUCUN attribut htmx — `:disabled` n'est
    pas une garde (DevTools réactive n'importe quel bouton, CLAUDE.md) et un
    `jump_prev_page` à None y émettrait « "page": "None" ».
    """
    for nom, ctx in _matrice():
        for ctrl in _BUTTON_RE.findall(_rendu(ctx)):
            if _DISABLED_RE.search(ctrl):
                assert "hx-" not in ctrl, (nom, ctrl[:160])
            else:
                assert '"page"' in ctrl and "hx-get" in ctrl, (nom, ctrl[:160])


def test_every_control_carries_hx_include_and_extra_vals():
    """Sans hx-include, un saut échange des heures sous un titre « Dépenses »."""
    html = _rendu(_cursor_ctx(JUMP_PAGES + 2, _GROS_PAGES * PAGE_SIZE,
                              extra_vals={"tab": "depenses"}))
    ctrls = _BUTTON_RE.findall(html)
    # Page 12 de 24 est le seul état où les six contrôles sont VIVANTS. Trois
    # tests s'appuyaient dessus sans le dire, donc sans le vérifier : épinglé.
    assert len(ctrls) == 6, ctrls
    assert not _inertes(html), _inertes(html)
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
    # Le filtre porte sur un LIBELLÉ, qu'un bouton grisé porte aussi : sans le
    # terme not-disabled, réemployer ce helper à une autre page dépasserait 4.
    sauts = [c for c in _actifs(html)
             if "page (" in c or "Première page" in c or "de %d pages" % JUMP_PAGES in c]
    assert len(sauts) == 4, sauts
    for ctrl in sauts:
        assert '"cursor": ""' in ctrl and '"trail": ""' in ctrl


# ── Les gardes d'affichage ─────────────────────────────────────────────────

def test_the_bar_is_hidden_for_a_single_page():
    """Les clés ajoutées ne doivent pas avoir desserré la garde extérieure."""
    assert _rendu(_page_ctx(PAGE_SIZE - 1, 1)).strip() == ""
    assert _rendu(None).strip() == ""


def test_a_missing_total_leaves_only_the_walk_and_the_way_home():
    """Compte illisible : ni « Fin », ni ±N — mais « Début » survit.

    L'ancienne version de ce test n'assertionnait RIEN sur « Première page »,
    donc elle restait verte que le contrôle parût ou non : la décision n'avait
    aucune épingle. Or page 1 EST le décalage 0, donc ce saut ne peut pas
    mentir sous un total faux, et c'est la SEULE sortie d'une marche profonde
    sans compte — « Précédent » avance d'une page et la traîne plafonne à
    MAX_TRAIL.
    """
    html = _rendu(_cursor_ctx(7, None))
    assert "Dernière page" not in html
    assert "de %d pages" % JUMP_PAGES not in html
    # La marche reste offerte — elle ne dépend d'aucun total.
    assert "Page précédente" in html and "Page suivante" in html
    assert "Première page" in html
    assert len(_BUTTON_RE.findall(html)) == 3, _empreinte(html)
    indicateur = " ".join(html.split())
    assert "Page 7 <" in indicateur or "Page 7</span>" in indicateur
    assert " / " not in indicateur, "aucun total ne doit paraître"


def test_the_total_is_shown_when_it_is_known():
    assert "Page 2 / 7" in " ".join(_rendu(_page_ctx(7 * PAGE_SIZE, 2)).split())


def test_every_control_is_a_button_with_an_aria_label():
    """Les <a> sans href n'étaient ni focusables, ni au clavier, ni annoncés.

    ms() rend `aria-hidden`, donc le glyphe ne contribue rien au nom
    accessible : chaque contrôle porte le sien. `title` était EXIGÉ par
    CLAUDE.md et épinglé nulle part : sans texte, le survol et l'appui long
    sont les deux seules façons d'apprendre ce que fait un bouton, et un carré
    gris sans nom est inidentifiable.
    """
    for nom, ctx in _matrice():
        html = _rendu(ctx)
        assert "<a " not in html, nom
        for ctrl in _BUTTON_RE.findall(html):
            assert _ARIA_RE.search(ctrl), (nom, ctrl[:120])
            assert 'title="' in ctrl, (nom, ctrl[:120])


# ── La propriété demandée : la rangée ne bouge pas ─────────────────────────

@pytest.mark.parametrize("pages", [2, 3, 4, JUMP_PAGES + 1, JUMP_PAGES + 2,
                                   JUMP_PAGES + 3, _GROS_PAGES])
def test_the_button_row_is_identical_across_a_full_walk_of_one_list(pages):
    """« Le bouton sous le doigt ne bouge jamais » — la propriété, pas le pixel.

    L'empreinte porte le COMPTE et l'ORDRE ; elle est invariante par
    construction sur un total fixe, les libellés des sauts ne citant que des
    destinations que le total détermine (« Dernière page (24) »).

    En mode PAGE, `paginate` écrête à [1..L] ; une marche en mode curseur
    dépasserait le total et changerait l'empreinte à bon droit (c'est ce que
    `test_a_control_that_applies_is_never_hidden` couvre). L=2 est le cas que
    `_matrice()` n'a pas : c'est là que les deux drapeaux de bouts servent.
    """
    empreintes = {_empreinte(_rendu(_page_ctx(pages * PAGE_SIZE, page)))
                  for page in range(1, pages + 1)}
    assert len(empreintes) == 1, empreintes


def test_a_countless_list_keeps_one_shape_too():
    """Compte illisible : trois contrôles, à toutes les pages, sans dérive."""
    empreintes = {_empreinte(_rendu(_cursor_ctx(page, None)))
                  for page in (1, 2, 3, 12, 40)}
    assert len(empreintes) == 1, empreintes


# ── Les deux bornes de chaque seuil ────────────────────────────────────────

@pytest.mark.parametrize("pages", [2, 3, 4, JUMP_PAGES + 1, JUMP_PAGES + 2,
                                   JUMP_PAGES + 3, _GROS_PAGES])
def test_no_list_renders_a_control_that_is_dead_on_every_one_of_its_pages(pages):
    """La thèse du lot : griser, oui ; dessiner un mort permanent, non.

    Attrape un seuil trop BAS — à L ≥ 11 les ±10 paraîtraient sans jamais
    pouvoir s'activer. Il ne peut PAS attraper un seuil trop haut : c'est le
    rôle du test de monotonie ci-dessous.
    """
    vus, actifs = set(), set()
    for page in range(1, pages + 1):
        html = _rendu(_page_ctx(pages * PAGE_SIZE, page))
        vus |= set(_empreinte(html))
        actifs |= {_ARIA_RE.search(b).group(0) for b in _actifs(html)}
    assert vus == actifs, (pages, sorted(vus - actifs))


@pytest.mark.parametrize("pages,page", [
    (1, 3), (2, 7), (3, 40), (JUMP_PAGES + 1, JUMP_PAGES + 2),
    (JUMP_PAGES + 2, 1), (JUMP_PAGES + 2, JUMP_PAGES + 2), (_GROS_PAGES, 12),
])
def test_a_control_that_applies_is_never_hidden(pages, page):
    """`enabled ⟹ rendered`. Les deux colonnes ne sont PAS indépendantes.

    En mode curseur `page` n'est PAS borné par `last_page` (un compte périmé
    ne doit pas figer un libellé dont les lignes avancent), donc
    `page > last_page` est un état de production atteignable — et un drapeau
    par LISTE calculé sur `last_page` pourrait y retirer un contrôle qui
    s'applique. Mesuré : QUATRE de ces sept cas échouaient avant ce lot —
    (1,3) et (2,7) perdaient « Début », (3,40) et (11,12) perdaient le « −10 »
    (page 12 d'une liste de 11 est le même dépassement).
    """
    ctx = _cursor_ctx(page, pages * PAGE_SIZE)
    html = _rendu(ctx)
    if ctx["show_first"]:
        assert "Première page" in html
    if ctx["show_end"]:
        assert "Dernière page" in html
    if ctx["jump_prev_page"]:
        assert "Reculer de" in html
    if ctx["jump_next_page"]:
        assert "Avancer de" in html


# ── Le contrôle grisé : inerte, et toujours nommé ──────────────────────────

def test_a_disabled_control_carries_no_hx_attribute_and_still_names_itself():
    """Page 1 d'une liste de 7 : « Début » et « Précédent » sont grisés.

    `:disabled` SEUL n'est pas une garde — « dans la FONCTION, pas seulement
    par `:disabled` : DevTools réactive n'importe quel bouton » (CLAUDE.md).
    Et `hover:` s'applique à un élément désactivé (Tailwind n'ajoute aucun
    `:not(:disabled)`), donc un bouton inerte s'allumerait au survol — dans
    `@media (hover:hover)`, si bien que le défaut serait INVISIBLE sur le
    téléphone du praticien et visible sur l'écran du relecteur.
    """
    html = _rendu(_page_ctx(7 * PAGE_SIZE, 1))
    morts = _inertes(html)
    assert len(morts) == 2, _empreinte(html)
    for c in morts:
        assert "hx-" not in c, c[:160]
        # Les CLÉS JSON, jamais les mots nus : « cursor » est une sous-chaîne
        # de `disabled:cursor-not-allowed`, le même piège que `_DISABLED_RE`
        # désarme plus haut — il s'est refermé une deuxième fois en écrivant
        # ce test, ce qui dit assez qu'il faut nommer la règle et non l'éviter.
        assert '"page"' not in c, c[:160]
        assert '"cursor"' not in c, c[:160]
        assert _ARIA_RE.search(c), c[:160]
        assert 'title="' in c, c[:160]
        assert "hover:" not in c, c[:160]
        assert "cursor-pointer" not in c, c[:160]


# ── La géométrie et la rangée unique ───────────────────────────────────────

def test_the_row_is_six_controls_at_the_documented_touch_size():
    """La décision du praticien devient un test, pas un commentaire.

    `.ms-20` vaut 21 px (les noms .ms-N sont décalés de leur taille), donc
    `px-3 py-3` + 1 px de bordure fait 47 × 47 — au-dessus du minimum de 44 de
    la règle 9. Aucun nombre n'est écrit ici : c'est la classe qui décide.
    """
    html = _rendu(_page_ctx(_GROS_PAGES * PAGE_SIZE, 12))
    ctrls = _BUTTON_RE.findall(html)
    assert len(ctrls) == 6, _empreinte(html)
    for c in ctrls:
        assert "px-3" in c and "py-3" in c, c[:160]
        assert "px-4" not in c and "py-1.5" not in c, c[:160]
    assert "ms-20" in html


def test_the_bar_is_one_row_with_the_counter_below_it():
    """Une rangée, puis le compteur — dans CET ordre de source.

    L'ordre du DOM est l'ordre visuel (aucun `order-*` nulle part), donc la
    position du compteur s'épingle par sa place dans la source. `w-full` est
    porteur : une rangée dimensionnée par son contenu ne laisse rien à
    distribuer à `justify-between`, et les six boutons se colleraient.
    """
    html = _rendu(_page_ctx(_GROS_PAGES * PAGE_SIZE, 12))
    assert html.count("justify-between") == 1, html.count("justify-between")
    assert "w-full" in html and "flex-col" in html
    # Pas de gap sur la rangée : à 18 px de racine, 6×50 + 5×9 dépasserait la
    # largeur utile et écrêterait « Fin », dont le title porte la destination.
    assert "justify-center gap-2" not in html
    assert 'role="status"' in html
    assert html.index('role="status"') > html.rindex("</button>")
    # Les deux <span></span> d'espacement ont disparu avec justify-between.
    assert "<span></span>" not in html


def test_every_hx_vals_payload_is_valid_json_in_every_branch():
    """`hx-vals` est du JSON que htmx PARSE — une virgule de trop le tue.

    Les autres épingles cherchent des sous-chaînes ; celle-ci parse. Depuis
    que la charge est assemblée dans une macro à deux branches PLUS le bloc
    `extra` des onglets, une accolade manquante ne casserait aucune assertion
    de sous-chaîne — htmx avalerait simplement la requête, et les filtres ou
    le numéro de page disparaîtraient sans un mot dans la console.
    """
    for nom, ctx in _matrice():
        for charge in re.findall(r"hx-vals='([^']+)'", _rendu(ctx)):
            valeurs = json.loads(charge)          # lève si malformé
            assert "page" in valeurs, (nom, charge)
            # Une clé présente et vide EST le signal qui aiguille la requête
            # vers la branche de page absolue : elle ne doit jamais manquer
            # d'un contrôle de saut ni porter le mot « None ».
            assert valeurs["page"] != "None", (nom, charge)
