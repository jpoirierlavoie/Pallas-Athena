"""Les blocs de la théorie de la cause — ``utils/analyse_blocs.py`` (D8).

Le module est PUR : il localise l'entête et les blocs A à H d'une note
d'analyse, et ne devine jamais. On épingle ici :

1. la reconnaissance d'un titre de bloc — par sa LETTRE, quel que soit le
   titre que le juriste lui a donné ;
2. les blocs de code : un titre dans un bloc de code n'en est pas un, une
   clôture qui n'existe pas n'ouvre rien (le rendu de Python-Markdown) ;
3. les refus — bloc manquant, en double, hors d'ordre pour une réécriture
   complète — qui ne citent JAMAIS un titre ni un contenu ;
4. les opérations : remplacer, compléter, réécrire, et l'invariant qui les
   porte — tout ce qui est hors de la cible reste identique à l'octet ;
5. la linéarité (CWE-1333) : aucun ``re`` dans le module, et des entrées
   hostiles à la taille maximale d'une note se traitent en temps borné.
"""

import os
import pathlib
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from utils import analyse_blocs as ab  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    from models import note as note_model  # noqa: E402

ATHENA = pathlib.Path(__file__).resolve().parent.parent
SEED = note_model._ANALYSE_SEED


def _note(*blocs: str, entete: str = "# Théorie de la cause\n\nPréambule.\n") -> str:
    """A small analyse note: *blocs* are ``"A"``-style letters or full
    heading lines, each given a one-line body and the seed's separator."""
    parts = [entete, "\n---\n\n"]
    for b in blocs:
        heading = b if b.startswith("#") else f"## Bloc {b} — Titre {b}"
        parts.append(f"{heading}\n\nCorps {heading[-1]}.\n\n---\n\n")
    return "".join(parts)


FULL = _note(*"ABCDEFGH")


def _around(content: str, key: str) -> tuple[str, str]:
    """(everything before the body of *key*, everything from its tail on)."""
    zone = ab.parse(content).zone(key)
    return content[:zone.body_start], content[zone.body_end:]


# ══════════════════════════════════════════════════════════════════════
# 1. La graine, et ce qu'est un titre de bloc
# ══════════════════════════════════════════════════════════════════════


def test_the_seed_has_its_eight_blocs_in_order():
    parsed = ab.parse(SEED)
    assert [z.key for z in parsed.blocs] == list("ABCDEFGH")
    assert parsed.ok and parsed.editable and parsed.in_order
    assert parsed.gaps == ()
    assert parsed.entete.heading == "Théorie de la cause"
    # Each bloc's tail is the seed's separator; the body ends before it.
    for zone in parsed.blocs[:-1]:
        assert SEED[zone.body_end:zone.end] == "\n---\n\n"
        assert SEED[zone.start:zone.body_start].startswith(f"## Bloc {zone.key} — ")


@pytest.mark.parametrize("line, letter", [
    ("## Bloc A — Identification et cadre procédural", "A"),
    ("## Bloc C — Mon titre à moi", "C"),        # renamed after the letter
    ("## Bloc H", "H"),                          # nothing follows
    ("## Bloc B: Les faits", "B"),
    ("## Bloc D.", "D"),
    ("## bloc e — minuscules", "E"),              # any case
    ("## BLOC f", "F"),
    ("##\tBloc G", "G"),
    ("##   Bloc A — espaces", "A"),
    ("## Bloc A — espace insécable", "A"),
    ("## Bloc A## ", "A"),
    ("## Bloc Ab", None),                         # a longer word
    ("## Bloc A1", None),
    ("## Bloc I — hors de A–H", None),
    ("## Bloc", None),
    ("## Blocs A", None),
    ("### Bloc A", None),                         # level 3
    ("# Bloc A", None),                           # level 1
    ("##Bloc A", None),                           # no space after ##
    (" ## Bloc A", None),                         # not at column 0
    ("Bloc A", None),
])
def test_a_bloc_heading_is_recognised_by_its_letter(line, letter):
    assert ab._bloc_letter(line) == letter


def test_a_renamed_heading_is_kept_verbatim_by_a_replace():
    content = FULL.replace("## Bloc C — Titre C", "## Bloc C — Le titre du juriste")
    out = ab.replace_bloc(content, "C", "Nouveau corps.")
    assert "## Bloc C — Le titre du juriste\n\nNouveau corps.\n\n---\n\n" in out
    assert ab.structure(out)["blocs"][2]["heading"] == "Bloc C — Le titre du juriste"


# ══════════════════════════════════════════════════════════════════════
# 2. Les blocs de code
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("fence", ["```", "~~~", "````", "```markdown"])
def test_a_heading_inside_a_fenced_block_is_not_a_bloc(fence):
    closer = fence.rstrip("markdown") or fence
    code = f"{fence}\n## Bloc D — exemple dans du code\n# Titre\n{closer}\n"
    content = FULL.replace("Corps C.", "Corps C.\n\n" + code)
    parsed = ab.parse(content)
    assert parsed.editable, (parsed.missing, parsed.duplicates)
    assert [z.key for z in parsed.blocs] == list("ABCDEFGH")
    # The code block stays inside bloc C's body.
    assert "exemple dans du code" in parsed.zone("C").body(content)


def test_an_unclosed_fence_hides_nothing():
    """Python-Markdown renders an unclosed fence as TEXT, so the headings
    after it render as headings — they must count here too, or a bloc the
    reader sees would be reported missing (or worse, a duplicate hidden)."""
    content = FULL.replace("Corps B.", "Corps B.\n\n```\nsans clôture")
    parsed = ab.parse(content)
    assert parsed.ok


def test_a_longer_run_does_not_close_a_fence():
    """Python-Markdown closes on the SAME run: ```` does not close ```, so
    that block is unclosed — text — and the heading under it counts."""
    content = FULL.replace("Corps C.", "Corps C.\n\n```\ncode\n````")
    assert ab.parse(content).ok


def test_a_closing_fence_with_trailing_text_does_not_close():
    content = FULL.replace("Corps C.", "Corps C.\n\n```\ncode\n``` suite")
    assert ab.parse(content).ok


def test_a_backtick_fence_with_a_backtick_in_its_info_string_opens_nothing():
    content = FULL.replace("Corps C.", "Corps C.\n\n```a`b\n## Bloc D — x\n```")
    assert ab.parse(content).duplicates == ("D",)


# ══════════════════════════════════════════════════════════════════════
# 3. Refuser, jamais deviner
# ══════════════════════════════════════════════════════════════════════


def test_a_duplicated_bloc_is_refused_with_its_letter():
    content = FULL.replace("## Bloc F — Titre F", "## Bloc C — Titre SECRET-TITRE")
    parsed = ab.parse(content)
    assert parsed.duplicates == ("C",) and parsed.missing == ("F",)
    for op in (lambda: ab.replace_bloc(content, "C", "x"),
               lambda: ab.append_to_bloc(content, "C", "x")):
        with pytest.raises(ab.BlocStructureError) as exc:
            op()
        assert exc.value.code == "bloc_manquant"   # missing is reported first
        assert "SECRET" not in str(exc.value)


def test_a_duplicate_alone_is_refused_as_such():
    content = FULL + "## Bloc D — Titre CONFIDENTIEL\n\nDoublon.\n"
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.append_to_bloc(content, "D", "x")
    assert exc.value.code == "bloc_en_double" and exc.value.letters == ("D",)
    assert "CONFIDENTIEL" not in str(exc.value) and "Doublon" not in str(exc.value)


def test_a_missing_heading_refuses_even_an_operation_on_another_bloc():
    """Deleting « ## Bloc F » moves F's text into E's zone: « the end of
    bloc E » is then a guess, so every bloc operation refuses."""
    content = FULL.replace("## Bloc F — Titre F\n", "")
    for key in ("E", "A", "entete"):
        with pytest.raises(ab.BlocStructureError) as exc:
            ab.append_to_bloc(content, key, "Ajout.")
        assert exc.value.code == "bloc_manquant" and exc.value.letters == ("F",)


def test_an_unknown_bloc_is_refused():
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.replace_bloc(FULL, "I", "x")
    assert exc.value.code == "bloc_inconnu"


def test_blocs_out_of_order_are_editable_but_not_a_valid_rewrite():
    content = _note(*"ABCEDFGH")
    parsed = ab.parse(content)
    assert parsed.editable and not parsed.in_order and not parsed.ok
    out = ab.append_to_bloc(content, "D", "Ajout.")
    assert "Corps D.\n\nAjout.\n\n---" in out
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.validate_full_rewrite(content)
    assert exc.value.code == "ordre"


def test_validate_full_rewrite_accepts_the_seed_and_names_what_is_missing():
    assert ab.validate_full_rewrite(SEED).ok
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.validate_full_rewrite(_note(*"ABDEFH"))
    assert exc.value.code == "bloc_manquant" and exc.value.letters == ("C", "G")
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.validate_full_rewrite(_note(*"ABCDEFGHA"))
    assert exc.value.code == "bloc_en_double" and exc.value.letters == ("A",)
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.validate_full_rewrite("Une note sans aucun bloc.")
    assert exc.value.letters == tuple("ABCDEFGH")


def test_every_refusal_is_a_value_error_with_a_french_message():
    assert issubclass(ab.BlocStructureError, ValueError)
    for code in ab._MESSAGES:
        message = str(ab.BlocStructureError(code, ("C",)))
        assert message and "{letters}" not in message


# ══════════════════════════════════════════════════════════════════════
# 4. Les opérations
# ══════════════════════════════════════════════════════════════════════


def test_replace_changes_the_body_only():
    before, after = _around(SEED, "D")
    out = ab.replace_bloc(SEED, "D", "La majeure, la mineure, la conclusion.")
    assert out.startswith(before) and out.endswith(after)
    assert ab.parse(out).zone("D").body(out) == (
        "\nLa majeure, la mineure, la conclusion.\n"
    )
    assert ab.parse(out).ok


def test_replace_trims_blank_edges_and_keeps_inner_text():
    out = ab.replace_bloc(FULL, "B", "\n\n  Retrait gardé.\n\nSuite.\n\n\n")
    assert "## Bloc B — Titre B\n\n  Retrait gardé.\n\nSuite.\n\n---" in out


def test_replace_with_nothing_empties_the_bloc_and_keeps_its_heading():
    out = ab.replace_bloc(FULL, "E", "")
    assert "## Bloc E — Titre E\n\n---\n\n## Bloc F" in out
    assert ab.parse(out).ok


def test_append_keeps_the_existing_body_byte_for_byte():
    zone = ab.parse(SEED).zone("F")
    out = ab.append_to_bloc(SEED, "F", "Nouvel argument.",
                            stamp="*Ajouté par Claude le 27 septembre 2026*")
    assert out[:zone.body_end] == SEED[:zone.body_end]
    assert out.endswith(SEED[zone.body_end:])
    inserted = out[zone.body_end:len(out) - (len(SEED) - zone.body_end)]
    assert inserted == (
        "\n*Ajouté par Claude le 27 septembre 2026*\n\nNouvel argument.\n"
    )
    assert ab.parse(out).ok


def test_append_never_creates_a_setext_heading_before_a_separator():
    """A separator directly under the body's last line (no blank line)
    would turn the appended paragraph into a level-2 setext title."""
    content = FULL.replace("Corps C.\n\n---", "Corps C.\n---")
    out = ab.append_to_bloc(content, "C", "Ajout.")
    assert "Ajout.\n\n---" in out
    assert "Ajout.\n---" not in out


def test_append_to_an_empty_bloc_and_to_the_last_bloc_without_newline():
    content = "# T\n\n## Bloc A\n## Bloc B\n" + "".join(
        f"## Bloc {x}\n" for x in "CDEFG") + "## Bloc H\n\nFin sans saut"
    out = ab.append_to_bloc(content, "A", "Premier.")
    assert "## Bloc A\n\nPremier.\n\n## Bloc B" in out
    out = ab.append_to_bloc(content, "H", "Dernier.")
    assert out.endswith("## Bloc H\n\nFin sans saut\n\nDernier.\n")


def test_an_empty_append_changes_nothing():
    assert ab.append_to_bloc(FULL, "A", "\n  \n") == FULL


def test_the_entete_keeps_its_title_and_its_separator():
    out = ab.replace_bloc(SEED, "entete", "*Dossier : 2026-001*\n\nPréambule revu.")
    assert out.startswith("# Théorie de la cause\n\n*Dossier : 2026-001*\n\n"
                          "Préambule revu.\n\n---\n\n## Bloc A — ")
    assert ab.parse(out).ok


def test_an_entete_without_a_title_is_replaced_from_the_first_line():
    content = FULL.replace("# Théorie de la cause\n\n", "")
    out = ab.replace_bloc(content, "entete", "Autre préambule.")
    assert out.startswith("Autre préambule.\n\n---\n\n## Bloc A")


def test_interstitial_text_survives_an_operation_on_its_neighbour():
    content = FULL.replace("## Bloc D", "## Annexe du juriste\n\nNotes libres.\n\n## Bloc D")
    parsed = ab.parse(content)
    assert parsed.ok and len(parsed.gaps) == 1
    out = ab.replace_bloc(content, "C", "Remplacé.")
    assert "## Annexe du juriste\n\nNotes libres.\n\n## Bloc D" in out
    assert ab.structure(out)["interstitial_chars"] == ab.structure(content)["interstitial_chars"]


@pytest.mark.parametrize("text", [
    "# Un titre de niveau 1",
    "## Un intertitre de niveau 2",
    "## Bloc D — une seconde fois",
    "#mot-clic",                                  # Python-Markdown: an H1
    "Paragraphe\n---",                            # setext H2
    "Paragraphe\n===",                            # setext H1
    "Paragraphe\n-",
])
def test_inserted_text_may_not_carry_a_structural_heading(text):
    for op in (lambda: ab.replace_bloc(FULL, "C", text),
               lambda: ab.append_to_bloc(FULL, "C", text)):
        with pytest.raises(ab.BlocStructureError) as exc:
            op()
        assert exc.value.code == "titre_structurant"


@pytest.mark.parametrize("text", [
    "### Un intertitre",
    "#### Plus bas",
    "---\n\nUne ligne après un séparateur.",
    "Texte.\n\n---\n\nSuite.",
    "```\n## Bloc D — dans du code, sans danger\n```",
    "| a | b |\n|---|---|\n| 1 | 2 |",
    "> ## citation",
])
def test_inserted_text_may_carry_subtitles_code_and_tables(text):
    out = ab.append_to_bloc(FULL, "C", text)
    assert ab.parse(out).ok


@pytest.mark.parametrize("text", [
    "```\ncode sans clôture",
    "~~~\ncode",
    "```\na\n```\n```",                            # a stray fence line
])
def test_inserted_text_may_not_leave_a_fence_open(text):
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.append_to_bloc(FULL, "C", text)
    assert exc.value.code == "bloc_de_code_ouvert"


def test_a_balanced_fence_that_pairs_with_an_open_one_above_is_refused():
    """The pre-checks pass — the inserted text closes its own block — but
    the lawyer left a fence open in bloc B: that fence now pairs with the
    inserted one and swallows « ## Bloc C ». Only the re-parse sees it."""
    content = FULL.replace("Corps B.", "Corps B.\n\n```\noublié ouvert")
    assert ab.parse(content).ok
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.append_to_bloc(content, "C", "```\nexemple\n```")
    assert exc.value.code == "structure_modifiee"


def test_a_stamp_must_be_one_line():
    for stamp in ("deux\nlignes", "   ", "## titre"):
        with pytest.raises(ab.BlocStructureError):
            ab.append_to_bloc(FULL, "A", "x", stamp=stamp)


def test_apply_operations_runs_in_order_and_refuses_as_a_whole():
    out = ab.apply_operations(SEED, [
        {"bloc": "A", "mode": "append", "content": "Partie ajoutée."},
        {"bloc": "G", "mode": "replace", "content": "Le thème : l'équité."},
        {"bloc": "entete", "mode": "replace", "content": "Préambule."},
    ], stamp="*Ajouté par Claude*")
    parsed = ab.parse(out)
    assert parsed.ok
    assert "*Ajouté par Claude*\n\nPartie ajoutée." in parsed.zone("A").body(out)
    assert parsed.zone("G").body(out) == "\nLe thème : l'équité.\n"
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.apply_operations(SEED, [
            {"bloc": "A", "mode": "append", "content": "ok"},
            {"bloc": "B", "mode": "append", "content": "## titre"},
        ])
    assert exc.value.code == "titre_structurant"
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.apply_operations(SEED, [
            {"bloc": "C", "mode": "append", "content": "1"},
            {"bloc": "C", "mode": "replace", "content": "2"},
        ])
    assert exc.value.code == "operations_en_double" and exc.value.letters == ("C",)
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.apply_operations(SEED, [{"bloc": "A", "mode": "insert", "content": ""}])
    assert exc.value.code == "mode_inconnu"
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.apply_operations(SEED, [{"bloc": "Z", "mode": "append", "content": ""}])
    assert exc.value.code == "bloc_inconnu"


def test_the_structure_summary_names_titles_and_sizes():
    summary = ab.structure(SEED)
    assert summary["ok"] is True and summary["missing"] == []
    assert [b["bloc"] for b in summary["blocs"]] == list("ABCDEFGH")
    assert summary["blocs"][0]["heading"].startswith("Bloc A — ")
    zone = ab.parse(SEED).zone("B")
    assert summary["blocs"][1]["chars"] == zone.body_end - zone.body_start
    assert summary["entete"]["heading"] == "Théorie de la cause"
    broken = ab.structure(_note(*"ABCDEFG"))
    assert broken["ok"] is False and broken["missing"] == ["H"]


# ══════════════════════════════════════════════════════════════════════
# 5. Fins de ligne et linéarité
# ══════════════════════════════════════════════════════════════════════


def test_crlf_content_is_parsed_and_preserved_outside_the_target():
    """A browser textarea posts \\r\\n: the parser must cut there, and an
    operation must leave every other byte — endings included — alone."""
    crlf = SEED.replace("\n", "\r\n")
    parsed = ab.parse(crlf)
    assert parsed.ok
    before, after = _around(crlf, "C")
    out = ab.replace_bloc(crlf, "C", "Remplacé.")
    assert out.startswith(before) and out.endswith(after)
    assert ab.parse(out).ok


def test_a_lone_carriage_return_is_a_line_break():
    content = FULL.replace("\n", "\r")
    assert ab.parse(content).ok


def test_a_unicode_line_separator_is_not_a_line_break():
    """str.splitlines would cut at U+2028; the renderer does not, so a
    heading-looking text after it is NOT a heading."""
    content = FULL.replace("Corps C.", "Corps C. ## Bloc D — collé")
    assert ab.parse(content).ok


def test_the_module_uses_no_regular_expression():
    """The CWE-1333 doctrine of utils/docx_fill: a scanner, not a pattern.
    Read from the AST, so the docstring may still NAME the doctrine."""
    import ast

    tree = ast.parse((ATHENA / "utils" / "analyse_blocs.py").read_text(
        encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported, "the sweep read no import at all"
    assert not imported & {"re", "regex"}, imported


_CAP = note_model.CONTENT_MAX_LENGTH


_HOSTILE = {
    "fences": lambda: "```\n" * (_CAP // 4),
    "growing-runs": lambda: "".join("`" * k + "\n" for k in range(3, 440)),
    "growing-runs-closed": lambda: "".join(
        ("`" * k + "\n") * 2 for k in range(3, 310)),
    "hashes": lambda: "#\n" * (_CAP // 2),
    "carriage-returns": lambda: "\r" * _CAP,
    "one-backtick-line": lambda: "`" * _CAP,
    "one-hash-line": lambda: "#" * _CAP,
    "bloc-headings": lambda: "## Bloc A\n" * (_CAP // 10),
    "almost-headings": lambda: ("## Bloc" + " " * 40 + "\n") * (_CAP // 48),
    "breaks": lambda: "---\n\n" * (_CAP // 5),
    "mixed-endings": lambda: "a\r\nb\rc\n" * (_CAP // 7),
}


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_hostile_inputs_parse_in_bounded_time(name):
    content = _HOSTILE[name]()
    assert len(content) <= _CAP + 10
    start = time.perf_counter()
    ab.structure(content)
    try:
        ab.append_to_bloc(content, "A", "x")
    except ab.BlocStructureError:
        # A refusal is a legitimate answer to a hostile input: only the time
        # the operation takes, refused or not, is under test here.
        pass
    assert time.perf_counter() - start < 2.0, name


def _best_of_two(fn) -> float:
    best = float("inf")
    for _ in range(2):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize("unit", [
    "\r",            # every line break found by the cached find
    "```\n",         # every line a fence candidate
    "a\n## Bloc A\n",
])
def test_the_cost_grows_linearly_with_the_text(unit):
    """A wall-clock ceiling alone misses a quadratic scan done in C: a
    ``str.find`` restarted at every line is memchr-fast, and at the note's
    cap the Python loop around it dominates (measured: the ratio test at
    25 K → 100 K let a broken find cache through). At 50 K → 400 K
    characters a linear parser costs about 8×; that broken cache cost 30×
    and more. The module is pure — nothing caps what a caller hands it."""
    small = unit * (50_000 // len(unit))
    large = unit * (400_000 // len(unit))
    t_small = max(_best_of_two(lambda: ab.structure(small)), 1e-4)
    t_large = _best_of_two(lambda: ab.structure(large))
    assert t_large / t_small < 16, (t_small, t_large)


def test_an_operation_on_a_note_at_the_size_cap_is_fast():
    filler = ("Une ligne de raisonnement juridique assez longue. " * 2 + "\n") * (
        (_CAP - len(SEED)) // 103
    )
    big = SEED.replace("Cause d'action", filler + "Cause d'action")
    assert len(big) <= _CAP
    start = time.perf_counter()
    out = ab.apply_operations(big, [
        {"bloc": "C", "mode": "append", "content": "Ajout."},
        {"bloc": "H", "mode": "replace", "content": "Fin."},
    ])
    assert ab.parse(out).ok
    assert time.perf_counter() - start < 2.0


# ══════════════════════════════════════════════════════════════════════
# 6. Le moteur de rendu fait foi (revue du lot 1a, L3)
# ══════════════════════════════════════════════════════════════════════
#
# The module claims to see the headings the reader sees. Until the review
# no test compared it with the renderer, and the claim was false on one
# point: every fence-like line was taken for an OPENER, while Python-
# Markdown's grammar refuses « ``` du texte » or « ~~~ ~~~ » — it renders
# them as text, and the headings under them as headings. The module then
# hid a level-two heading the lawyer sees, so a bloc's zone ran over text
# the screen shows under ANOTHER heading, and a replace deleted it.


def _rendered_top_headings(content: str) -> list[tuple[str, str]]:
    """The level-1/2 headings the Analyse tab renders, in order."""
    import re as _re  # the TEST reads HTML; the module stays regex-free

    from utils.markdown_docx import markdown_to_safe_html

    html = markdown_to_safe_html(content)
    return [
        (level, _re.sub(r"<[^>]+>", "", text).strip())
        for level, text in _re.findall(r"<h([12])[^>]*>(.*?)</h\1>", html, _re.S)
    ]


def _module_top_headings(content: str) -> list[tuple[str, str]]:
    lines = ab._split_lines(content)
    fenced, _unpaired = ab._fence_marks(lines)
    out = []
    for i, line in enumerate(lines):
        level = ab._atx_level(line.text)
        if not fenced[i] and level in (1, 2):
            out.append((str(level), line.text.lstrip("#").strip().rstrip("#").strip()))
    return out


_FENCE_CASES = {
    "info-string-words": "```Voici du code\n## Sous-titre\ntexte\n```\n",
    "tilde-tilde": "~~~ ~~~\n## Sous-titre\n~~~\n",
    "language": "```python\n## pas un titre\n```\n",
    "language-plus": "```c++\n## pas un titre\n```\n",
    "dotted-language": "```.py\n## pas un titre\n```\n",
    "attrs": "``` {.python}\n## pas un titre\n```\n",
    "attrs-unclosed": "``` {python\n## Sous-titre\n```\n",
    "hl-lines": '```python hl_lines="1 2"\n## pas un titre\n```\n',
    "hl-lines-alone": "```hl_lines='1'\n## pas un titre\n```\n",
    "closer-with-tab": "```\n## pas un titre\n```\t\n",
    "opener-with-tab": "```\t\n## pas un titre\n```\n",
    "backtick-in-info": "```a`b\n## Sous-titre\n```\n",
    "words-after-language": "``` .py x\n## Sous-titre\n```\n",
}


@pytest.mark.parametrize("name", sorted(_FENCE_CASES))
def test_fence_openers_follow_the_renderer(name):
    content = "## Bloc C\n\nCorps.\n\n" + _FENCE_CASES[name] + "\n## Bloc D\n\nx\n"
    assert _module_top_headings(content) == _rendered_top_headings(content)


def test_headings_match_the_renderer_on_generated_notes():
    """A seeded corpus of fence, heading and text lines. Setext underlines
    are left out on purpose: the module does not take them for boundaries
    (documented), and the insertion checks refuse to create one."""
    import random

    pieces = [
        "", "texte", "autre ligne", "## Bloc A", "## Bloc B x", "# Titre",
        "## Sous", "### petit", "```", "```python", "``` du texte", "```c++",
        "``` {.py}", "```{x}", "~~~", "~~~ ~~~", "~~~js", "````", "```\t",
        "``` ", '```hl_lines="1 2"', '```python hl_lines="1"', "```a`b",
        "`` pas une clôture", "    ## en retrait", "#motclic", "``` {bad",
        "```.py", "``` .py x",
    ]
    rng = random.Random(20260927)
    for _ in range(1500):
        content = "\n".join(
            rng.choice(pieces) for _ in range(rng.randint(1, 12))
        ) + "\n"
        assert _module_top_headings(content) == _rendered_top_headings(content), (
            content
        )


def test_a_replace_never_deletes_text_under_a_heading_the_reader_sees():
    """The regression: « ```Voici du code » opens nothing on screen, so
    « ## Notes du juriste » is a heading there and the text under it is
    interstitial — outside every bloc. The module used to fence it into
    bloc C, and a replace of bloc C deleted it."""
    content = FULL.replace(
        "Corps C.",
        "Corps C.\n```Voici du code\nligne\n\n## Notes du juriste\n\n"
        "PRÉCIEUX\n```",
    )
    assert ab.parse(content).gaps, "the text under the heading is interstitial"
    out = ab.replace_bloc(content, "C", "Nouveau C.")
    assert "PRÉCIEUX" in out and "## Notes du juriste" in out
    assert "Corps C." not in out


def test_an_inserted_line_that_cannot_open_is_text_not_an_open_fence():
    """« ``` du texte » opens nothing on screen and cannot close anything:
    refusing it as « un bloc de code ouvert » would be false."""
    out = ab.append_to_bloc(FULL, "C", "``` du texte")
    assert ab.parse(out).ok


@pytest.mark.parametrize("underline", ["-=-", "=-", "---\t", "=== "])
def test_a_setext_underline_is_refused_mixed_or_with_trailing_blanks(underline):
    """Python-Markdown's underline is ``[=-]+[ ]*``: « -=- » makes an H2
    of the line above it on screen."""
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.append_to_bloc(FULL, "C", "Paragraphe\n" + underline)
    assert exc.value.code == "titre_structurant"


@pytest.mark.parametrize("op", [
    {"bloc": "C", "mode": "replace"},
    {"bloc": "C", "mode": "append"},
    "C",
])
def test_an_operation_without_content_is_refused_not_read_as_empty(op):
    """A forgotten ``content`` used to default to « » — a replace then
    EMPTIED the bloc. An empty string still empties it, on purpose."""
    with pytest.raises(ab.BlocStructureError) as exc:
        ab.apply_operations(FULL, [op])
    assert exc.value.code == "operation_invalide"
    emptied = ab.apply_operations(
        FULL, [{"bloc": "C", "mode": "replace", "content": ""}]
    )
    assert ab.parse(emptied).zone("C").body(emptied) == ""
