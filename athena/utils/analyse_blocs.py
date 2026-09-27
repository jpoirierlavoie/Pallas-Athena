"""The blocs of the « Théorie de la cause » — located, never guessed. PURE.

The dossier's analyse note (``models/note.py``, ``is_analyse``) is Markdown
seeded with a heading block and eight blocs, « ## Bloc A — … » to « ## Bloc
H — … ». Nothing in the application parsed them until this module: the
Analyse tab renders the raw Markdown, and the lawyer edits the whole text
through the ordinary note form — renaming a bloc's title after its letter,
adding rubrics, deleting a heading. Plan decision D8 lets the connector
replace or complete ONE bloc, or rewrite the whole note keeping the eight
headings; every such write needs a locator that is right, or refuses.

What a bloc is
--------------
A bloc heading is a line that STARTS with ``## Bloc <LETTER>`` — level two
exactly (``##`` then a space or tab), the word « Bloc » (any case), a
letter A–H (any case), then the end of the line or a character that is not
a letter or a digit. Whatever follows is the lawyer's title and is kept
verbatim: matching is on the LETTER. « ## Bloc A — Identification »,
« ## Bloc A: Cadre » and « ## bloc a » are bloc A; « ## Bloc Ab »,
« ### Bloc A » and « ##Bloc A » are not.

A bloc's ZONE runs from its heading to the next level-one or level-two
heading, or to the end of the text. Its BODY is the zone minus the heading
line and minus its TAIL — the trailing blank lines around at most one
thematic break (``---``, ``***``, ``___``), the separator the seed puts
between blocs. The ENTÊTE (key ``entete``) is everything before the first
bloc heading; a level-one heading on its first line is its « heading » and
is kept like a bloc's. A zone that ends on a level-one or level-two heading
that is NOT a bloc leaves a gap before the next bloc — « interstitial »
text, never touched by a bloc operation.

Headings are recognised the way the renderer recognises them — Python-
Markdown with ``fenced_code``, the pipeline of the Analyse tab and of the
Word print: a heading line starts at column 0 (``#foo`` is a level-one
heading there, so it is a boundary here too); a line inside a fenced code
block is never a heading. A fence opens on a column-0 run of three or more
backticks or tildes, and is a code block ONLY if a later line carries the
very same run followed by nothing but spaces — an unclosed fence renders as
text, and its « headings » render as headings, so they count. Setext
headings (a line underlined with ``===``/``---``) are not zone boundaries;
the insertion rules below keep an operation from ever creating one.

Refuse, never guess
-------------------
A bloc operation needs EVERY letter present exactly once
(:attr:`Structure.editable`). The rule is broader than « the target is
unique » on purpose: when a heading is missing, its content now sits in the
PREVIOUS bloc's zone — appending « at the end of bloc E » after the lawyer
deleted « ## Bloc F » would land after F's orphaned text, a location the
parser could only guess. Order is not required to edit (a reordered note
locates unambiguously), but :func:`validate_full_rewrite` requires it: a
full rewrite is the moment the eight headings are laid out as D8 fixes
them.

Every operation re-parses its result and refuses (``structure_modifiee``)
unless every segment outside its target — the other blocs, the entête, the
interstitial text — is byte-identical and the target's heading line is
unchanged. That post-check is the authority; the pre-checks on the inserted
text (no level-one or level-two heading, ATX or setext, outside a code
block; no fence left open) exist to name the reason. An inserted fence can,
for instance, pair with a fence the lawyer left unclosed further down the
note and swallow the next bloc's heading — only the re-parse can see that.

Refusals are :class:`BlocStructureError` (a ``ValueError``) carrying a
machine code and LETTERS only — never a heading's text or any content, so a
refusal can travel to a log line or a tool error without quoting privileged
prose (the discipline of ``mcp/handlers``).

Linearity (CWE-1333)
--------------------
No regular expression at all: every scan is a single forward pass over the
text, lines are cut with ``str.find`` whose search positions only move
forward, and the fence pairing walks each run's closer list once. The
worst case is linear in the text — ``tests/test_analyse_blocs.py`` times it
on adversarial inputs at the note's size cap and pins the absence of ``re``.
The doctrine is ``utils/docx_fill``'s: a ``.*?`` under ``re.DOTALL`` cost
this repository 45 seconds on 465 KB.

Stdlib only; no Firestore, no Flask, no model import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

BLOC_LETTERS: tuple[str, ...] = tuple("ABCDEFGH")
ENTETE = "entete"
ZONE_KEYS: tuple[str, ...] = (ENTETE, *BLOC_LETTERS)

MODES: tuple[str, ...] = ("replace", "append")

# Characters that may separate « Bloc » from its letter: ordinary blanks and
# the two no-break spaces French typography inserts.
_HEADING_BLANKS = " \t  "

_MESSAGES = {
    "bloc_inconnu": "Bloc inconnu : seuls « entete » et les lettres A à H existent.",
    "bloc_manquant": (
        "La théorie de la cause n'a pas ses huit blocs : titre manquant pour "
        "{letters}. Rétablissez les titres « ## Bloc <lettre> » (ou réécrivez "
        "la note entière) avant de modifier un bloc."
    ),
    "bloc_en_double": (
        "La théorie de la cause porte deux fois le titre du bloc {letters} : "
        "impossible de savoir lequel modifier."
    ),
    "ordre": (
        "Les huit titres « ## Bloc A » à « ## Bloc H » doivent paraître dans "
        "l'ordre, une seule fois chacun."
    ),
    "titre_structurant": (
        "Le texte contient un titre de niveau 1 ou 2 (une ligne commençant par "
        "« # » ou « ## », ou soulignée de « === » / « --- ») : il couperait le "
        "bloc. Utilisez « ### » ou plus pour les intertitres d'un bloc."
    ),
    "bloc_de_code_ouvert": (
        "Le texte ouvre un bloc de code (``` ou ~~~) sans le refermer : il "
        "avalerait la suite de la note."
    ),
    "mention_invalide": "La mention de provenance doit tenir sur une seule ligne.",
    "mode_inconnu": "Mode inconnu : « replace » ou « append ».",
    "contenu_invalide": "Le contenu d'une opération doit être du texte.",
    "operations_en_double": (
        "Une même opération ne peut viser deux fois {letters}."
    ),
    "structure_modifiee": (
        "La modification changerait la note hors du bloc visé ; rien n'a été "
        "appliqué."
    ),
}


class BlocStructureError(ValueError):
    """A bloc operation that cannot be applied without guessing.

    ``code`` is machine-stable; ``letters`` names the zones concerned. The
    message is French and never quotes content or heading text.
    """

    def __init__(self, code: str, letters: Iterable[str] = ()) -> None:
        self.code = code
        self.letters = tuple(letters)
        template = _MESSAGES.get(code, _MESSAGES["structure_modifiee"])
        super().__init__(template.format(letters=", ".join(self.letters) or "—"))


# ── Lines ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Line:
    start: int      # offset of the first character
    text_end: int   # offset just past the text, before the line ending
    end: int        # offset of the next line (== text_end at EOF)
    text: str


def _split_lines(content: str) -> list[_Line]:
    """Cut *content* at ``\\r\\n``, ``\\r`` and ``\\n`` — the three endings the
    renderer normalises — in ONE forward pass.

    Never ``str.splitlines``: it also cuts at U+2028, U+0085 and the form
    feed, which the renderer does not, so a pasted paragraph separator would
    move a heading the reader never sees. Each ``find`` resumes from the
    current position and its result is cached until passed, so no character
    is scanned twice per ending kind.
    """
    lines: list[_Line] = []
    n = len(content)
    pos = 0
    next_lf = content.find("\n")
    next_cr = content.find("\r")
    while pos < n:
        if next_lf != -1 and next_lf < pos:
            next_lf = content.find("\n", pos)
        if next_cr != -1 and next_cr < pos:
            next_cr = content.find("\r", pos)
        candidates = [p for p in (next_lf, next_cr) if p != -1]
        if not candidates:
            lines.append(_Line(pos, n, n, content[pos:n]))
            break
        brk = min(candidates)
        stop = brk + 2 if content.startswith("\r\n", brk) else brk + 1
        lines.append(_Line(pos, brk, stop, content[pos:brk]))
        pos = stop
    return lines


def _blank(text: str) -> bool:
    return text.strip(" \t") == ""


def _is_break(text: str) -> bool:
    """A thematic break: up to three spaces, then three or more of ONE of
    ``-``, ``*``, ``_``, blanks allowed between them, nothing else."""
    indent = len(text) - len(text.lstrip(" "))
    if indent > 3:
        return False
    marks = text.replace(" ", "").replace("\t", "")
    return len(marks) >= 3 and marks[0] in "-*_" and marks == marks[0] * len(marks)


def _atx_level(text: str) -> int:
    """The level of a column-0 ATX heading, 0 when the line is not one.

    Python-Markdown needs no space after the hashes (« #foo » is a level-one
    heading there), so neither does a BOUNDARY; a bloc heading is stricter
    (:func:`_bloc_letter`).
    """
    n = 0
    while n < len(text) and n < 7 and text[n] == "#":
        n += 1
    return n if 1 <= n <= 6 else 0


def _bloc_letter(text: str) -> Optional[str]:
    """The letter of a « ## Bloc X » heading line, or ``None``."""
    if not (text.startswith("##") and len(text) > 2 and text[2] in " \t"):
        return None
    rest = text[3:].lstrip(" \t")
    if rest[:4].casefold() != "bloc" or len(rest) < 6 or rest[4] not in _HEADING_BLANKS:
        return None
    k = 5
    while k < len(rest) and rest[k] in _HEADING_BLANKS:
        k += 1
    if k >= len(rest):
        return None
    letter = rest[k].upper()
    if letter not in BLOC_LETTERS:
        return None
    follower = rest[k + 1:k + 2]
    if follower and follower.isalnum():
        return None
    return letter


def _heading_text(text: str) -> str:
    """A heading line's title, for DISPLAY: the leading hashes and the
    optional closing sequence removed."""
    body = text.lstrip("#").strip(" \t")
    return body.rstrip("#").rstrip(" \t") if body.rstrip("#") else body


# ── Fences ───────────────────────────────────────────────────────────────


def _fence_run(text: str) -> Optional[str]:
    if not text or text[0] not in "`~":
        return None
    ch = text[0]
    n = 0
    while n < len(text) and text[n] == ch:
        n += 1
    return text[:n] if n >= 3 else None


def _fence_marks(lines: list[_Line]) -> tuple[list[bool], list[bool]]:
    """``(fenced, unpaired)`` per line.

    ``fenced[i]`` — line *i* is a fence line or inside a code block, so it
    can be neither a heading nor a separator. ``unpaired[i]`` — line *i*
    looks like a fence but no code block uses it (the renderer shows it as
    text).

    Python-Markdown's pairing, reproduced in one pass: scanning top-down,
    the first fence-like line that has a LATER line with the same run and
    nothing but spaces after it opens a block ending on the first such
    line; the scan resumes after it.
    """
    runs = [_fence_run(line.text) for line in lines]
    closers: dict[str, list[int]] = {}
    for i, (line, run) in enumerate(zip(lines, runs)):
        if run is not None and line.text[len(run):].strip(" ") == "":
            closers.setdefault(run, []).append(i)
    pointer: dict[str, int] = {}
    fenced = [False] * len(lines)
    unpaired = [False] * len(lines)
    i = 0
    while i < len(lines):
        run = runs[i]
        if run is None:
            i += 1
            continue
        rest = lines[i].text[len(run):]
        candidates = closers.get(run, [])
        p = pointer.get(run, 0)
        while p < len(candidates) and candidates[p] <= i:
            p += 1
        pointer[run] = p
        opens = not (run[0] == "`" and "`" in rest)
        if opens and p < len(candidates):
            j = candidates[p]
            for k in range(i, j + 1):
                fenced[k] = True
            i = j + 1
            continue
        unpaired[i] = True
        i += 1
    return fenced, unpaired


# ── Structure ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Zone:
    """One zone of the note, as offsets into the parsed text.

    ``[start, body_start)`` is the heading line (empty for an entête
    without a level-one title), ``[body_start, body_end)`` the body,
    ``[body_end, end)`` the tail.
    """

    key: str
    start: int
    body_start: int
    body_end: int
    end: int
    heading: str

    def body(self, content: str) -> str:
        return content[self.body_start:self.body_end]


@dataclass(frozen=True)
class Structure:
    content_length: int
    entete: Zone
    blocs: tuple[Zone, ...]                 # document order, duplicates kept
    gaps: tuple[tuple[int, int], ...]       # interstitial text
    missing: tuple[str, ...]
    duplicates: tuple[str, ...]
    in_order: bool

    @property
    def editable(self) -> bool:
        """Every letter present exactly once: a bloc operation may run."""
        return not self.missing and not self.duplicates

    @property
    def ok(self) -> bool:
        """Editable AND in the order D8 lays them out."""
        return self.editable and self.in_order

    def zone(self, key: str) -> Zone:
        """The zone *key* names; raises unless the structure is editable."""
        if key not in ZONE_KEYS:
            raise BlocStructureError("bloc_inconnu")
        if self.missing:
            raise BlocStructureError("bloc_manquant", self.missing)
        if self.duplicates:
            raise BlocStructureError("bloc_en_double", self.duplicates)
        if key == ENTETE:
            return self.entete
        return next(z for z in self.blocs if z.key == key)


def _body_bounds(
    lines: list[_Line], first: int, stop: int, fenced: list[bool],
    body_start: int, zone_end: int,
) -> int:
    """The offset where the body of lines ``[first, stop)`` ends — before
    its trailing blank lines and at most one thematic break."""
    k = stop
    while k > first and _blank(lines[k - 1].text):
        k -= 1
    if k > first and not fenced[k - 1] and _is_break(lines[k - 1].text):
        k -= 1
        while k > first and _blank(lines[k - 1].text):
            k -= 1
    if k == stop:
        return zone_end
    return lines[k].start if k > first else body_start


def parse(content: str) -> Structure:
    """Locate the entête and the blocs of *content*. Never raises: what is
    missing or duplicated is REPORTED, and the operations refuse on it."""
    lines = _split_lines(content)
    fenced, _unpaired = _fence_marks(lines)
    n = len(content)

    boundaries: list[int] = []          # line indexes of level-1/2 headings
    letters: dict[int, str] = {}        # line index → bloc letter
    for i, line in enumerate(lines):
        if fenced[i]:
            continue
        level = _atx_level(line.text)
        if level in (1, 2):
            boundaries.append(i)
            letter = _bloc_letter(line.text) if level == 2 else None
            if letter is not None:
                letters[i] = letter

    def line_start(idx: int) -> int:
        return lines[idx].start if idx < len(lines) else n

    bloc_lines = sorted(letters)
    first_bloc = bloc_lines[0] if bloc_lines else len(lines)

    # The entête: [0, first bloc heading).
    entete_end = line_start(first_bloc)
    if first_bloc > 0 and not fenced[0] and _atx_level(lines[0].text) == 1:
        head_line, e_body_start = lines[0], lines[0].end
        e_heading = _heading_text(head_line.text)
        e_first = 1
    else:
        e_body_start, e_heading, e_first = 0, "", 0
    e_body_end = _body_bounds(lines, e_first, first_bloc, fenced,
                              e_body_start, entete_end)
    entete = Zone(ENTETE, 0, e_body_start, max(e_body_start, e_body_end),
                  entete_end, e_heading)

    # Each bloc zone ends at the next level-1/2 heading (bloc or not).
    next_boundary: dict[int, int] = {}
    for a, b in zip(boundaries, boundaries[1:] + [len(lines)]):
        next_boundary[a] = b
    blocs: list[Zone] = []
    gaps: list[tuple[int, int]] = []
    for idx_pos, i in enumerate(bloc_lines):
        stop = next_boundary[i]
        zone_end = line_start(stop)
        body_start = lines[i].end
        body_end = _body_bounds(lines, i + 1, stop, fenced, body_start, zone_end)
        blocs.append(Zone(letters[i], lines[i].start, body_start,
                          max(body_start, body_end), zone_end,
                          _heading_text(lines[i].text)))
        following = (bloc_lines[idx_pos + 1] if idx_pos + 1 < len(bloc_lines)
                     else len(lines))
        if stop < following:
            # A non-bloc level-1/2 heading ends this zone: the text up to
            # the next bloc (or the end) is interstitial.
            gaps.append((zone_end, line_start(following)))

    seen = [z.key for z in blocs]
    missing = tuple(x for x in BLOC_LETTERS if x not in seen)
    duplicates = tuple(x for x in BLOC_LETTERS if seen.count(x) > 1)
    in_order = seen == list(BLOC_LETTERS)
    return Structure(n, entete, tuple(blocs), tuple(gaps), missing,
                     duplicates, in_order)


def structure(content: str) -> dict:
    """A READ summary: which blocs exist, their titles, their sizes.

    Heading text is included (it is the note's own content, returned to a
    caller already allowed to read it); character counts are of the BODY.
    """
    parsed = parse(content)
    return {
        "ok": parsed.ok,
        "editable": parsed.editable,
        "in_order": parsed.in_order,
        "missing": list(parsed.missing),
        "duplicates": list(parsed.duplicates),
        "entete": {
            "heading": parsed.entete.heading,
            "chars": parsed.entete.body_end - parsed.entete.body_start,
        },
        "blocs": [
            {"bloc": z.key, "heading": z.heading,
             "chars": z.body_end - z.body_start}
            for z in parsed.blocs
        ],
        "interstitial_chars": sum(b - a for a, b in parsed.gaps),
    }


# ── Checks on inserted text ──────────────────────────────────────────────


def check_insertable(text: str) -> None:
    """Refuse text that would restructure the note once inserted.

    No level-one or level-two heading outside a code block — ATX
    (« # », « ## », « #foo ») or setext (a non-blank line followed by a line
    of ``=`` or ``-`` only) — and no fence-like line left unpaired: an
    unclosed fence could pair with one further down the note.
    """
    lines = _split_lines(text)
    fenced, unpaired = _fence_marks(lines)
    if any(unpaired):
        raise BlocStructureError("bloc_de_code_ouvert")
    previous_text = False
    for i, line in enumerate(lines):
        if fenced[i]:
            previous_text = False
            continue
        if _atx_level(line.text) in (1, 2):
            raise BlocStructureError("titre_structurant")
        stripped = line.text.rstrip(" ")
        if (previous_text and stripped and stripped[0] in "=-"
                and stripped == stripped[0] * len(stripped)):
            raise BlocStructureError("titre_structurant")
        previous_text = not _blank(line.text)


def _check_stamp(stamp: Optional[str]) -> None:
    if stamp is None:
        return
    if "\n" in stamp or "\r" in stamp or _blank(stamp):
        raise BlocStructureError("mention_invalide")
    check_insertable(stamp)


def _trim_blank_edges(text: str) -> str:
    """*text* without its leading and trailing blank lines (inner content
    and the first line's indentation kept)."""
    lines = _split_lines(text)
    lo, hi = 0, len(lines)
    while lo < hi and _blank(lines[lo].text):
        lo += 1
    while hi > lo and _blank(lines[hi - 1].text):
        hi -= 1
    if lo == hi:
        return ""
    return text[lines[lo].start:lines[hi - 1].text_end]


# ── Operations ───────────────────────────────────────────────────────────


def _ends_line(prefix: str) -> bool:
    return not prefix or prefix.endswith(("\n", "\r"))


def _starts_with_nonblank_line(following: str) -> bool:
    """True when the first line of *following* has visible text."""
    stops = [p for p in (following.find("\n"), following.find("\r")) if p != -1]
    first = following[:min(stops)] if stops else following
    return bool(following) and not _blank(first)


def _segments(content: str, parsed: Structure, targets: set[str]) -> list:
    """The note as an ordered list of comparable pieces: every segment
    outside the targets whole, each target by its heading line only."""
    pieces: list = []
    z = parsed.entete
    if ENTETE in targets:
        pieces.append(("head", ENTETE, content[z.start:z.body_start]))
    else:
        pieces.append(("zone", ENTETE, content[z.start:z.end]))
    gaps = {a: b for a, b in parsed.gaps}
    for zone in parsed.blocs:
        if zone.key in targets:
            pieces.append(("head", zone.key, content[zone.start:zone.body_start]))
        else:
            pieces.append(("zone", zone.key, content[zone.start:zone.end]))
        if zone.end in gaps:
            pieces.append(("gap", "", content[zone.end:gaps[zone.end]]))
    return pieces


def _post_check(before: str, after: str, targets: set[str]) -> None:
    old, new = parse(before), parse(after)
    if not new.editable:
        raise BlocStructureError("structure_modifiee")
    if [z.key for z in new.blocs] != [z.key for z in old.blocs]:
        raise BlocStructureError("structure_modifiee")
    if _segments(before, old, targets) != _segments(after, new, targets):
        raise BlocStructureError("structure_modifiee")


def replace_bloc(content: str, key: str, text: str) -> str:
    """*content* with the body of zone *key* replaced by *text*.

    The heading line and the tail (the separator before the next bloc) are
    kept verbatim; *text* loses its leading and trailing blank lines, and a
    blank line always separates it from the heading and from a following
    separator (so it can never become the title of a setext heading).
    """
    zone = parse(content).zone(key)
    check_insertable(text)
    body = _trim_blank_edges(text)
    prefix = content[:zone.body_start]
    following = content[zone.body_end:]
    has_heading = zone.body_start > zone.start
    region = ""
    if body:
        if has_heading:
            region = ("" if _ends_line(prefix) else "\n") + "\n" + body + "\n"
        else:
            region = body + "\n"
        if _starts_with_nonblank_line(following):
            region += "\n"
    elif not _ends_line(prefix):
        region = "\n"
    after = prefix + region + following
    _post_check(content, after, {key})
    return after


def append_to_bloc(
    content: str, key: str, text: str, *, stamp: Optional[str] = None,
) -> str:
    """*content* with *text* added at the END of zone *key*'s body.

    The existing body is kept byte for byte; a blank line separates the
    addition from it and from a following separator. *stamp* — an optional
    ONE-line provenance mention (the caller's words, e.g. « *Ajouté par
    Claude le …* ») — is placed on its own paragraph before the text. It is
    never a ``---`` separator: inside a bloc that would read as a bloc
    boundary.
    """
    zone = parse(content).zone(key)
    check_insertable(text)
    _check_stamp(stamp)
    body = _trim_blank_edges(text)
    if not body:
        return content
    prefix = content[:zone.body_end]
    following = content[zone.body_end:]
    has_heading = zone.body_start > zone.start
    empty_body = zone.body_end == zone.body_start
    lead = "" if _ends_line(prefix) else "\n"
    if not (empty_body and not has_heading):
        lead += "\n"
    addition = lead + (f"{stamp}\n\n" if stamp is not None else "") + body + "\n"
    if _starts_with_nonblank_line(following):
        addition += "\n"
    after = prefix + addition + following
    _post_check(content, after, {key})
    return after


def apply_operations(
    content: str, operations: Iterable[dict], *, stamp: Optional[str] = None,
) -> str:
    """Apply ``[{"bloc": key, "mode": "replace"|"append", "content": str}]``
    in order, each on the result of the previous one — the whole list or
    nothing (a refusal raises before anything is returned).

    A key may appear once per call: two operations on one bloc have no
    single order a reader could predict.
    """
    ops = list(operations)
    keys = [op.get("bloc") for op in ops]
    for key in keys:
        if key not in ZONE_KEYS:
            raise BlocStructureError("bloc_inconnu")
    doubled = [k for k in ZONE_KEYS if keys.count(k) > 1]
    if doubled:
        raise BlocStructureError("operations_en_double", doubled)
    result = content
    for op in ops:
        mode = op.get("mode")
        text = op.get("content", "")
        if not isinstance(text, str):
            raise BlocStructureError("contenu_invalide")
        if mode == "replace":
            result = replace_bloc(result, op["bloc"], text)
        elif mode == "append":
            result = append_to_bloc(result, op["bloc"], text, stamp=stamp)
        else:
            raise BlocStructureError("mode_inconnu")
    return result


def validate_full_rewrite(content: str) -> Structure:
    """A full rewrite keeps the eight headings: each of « ## Bloc A » to
    « ## Bloc H » exactly once, in that order. Returns the structure, or
    raises naming the letters at fault (missing first, then duplicated)."""
    parsed = parse(content)
    if parsed.missing:
        raise BlocStructureError("bloc_manquant", parsed.missing)
    if parsed.duplicates:
        raise BlocStructureError("bloc_en_double", parsed.duplicates)
    if not parsed.in_order:
        raise BlocStructureError("ordre")
    return parsed
