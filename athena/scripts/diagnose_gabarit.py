"""Diagnose a .docx gabarit locally — list its placeholders, how each one is
classified (auto / saisie / Word), and any field still fragmented by Word,
with the likely structural cause.

    python -m scripts.diagnose_gabarit chemin/vers/gabarit.docx

Imports only ``utils.docx_fill`` + ``utils.template_fields`` (pure stdlib),
so it runs without the Firestore/Flask runtime. Read-only and prints to
stdout only — safe to run on a real template (nothing is logged or uploaded).

Since the July 2026 run-normalization fix, formatting / language / revision
splits heal automatically (including the frequent split at the dot in a
namespaced name like ``{{dossier.defendeur}}``). A field that is STILL
reported here has a *structural* split — a line break, a Word field, a
bookmark, or an image sits inside the ``{{ }}`` — which retyping the field
in one stroke (without that element) fixes.
"""

import io
import re
import sys
import zipfile

from utils.docx_fill import _normalize_runs, validate_template
from utils.template_fields import classify_placeholders

# The XML parts a placeholder can live in.
_TARGET_RE = re.compile(r"^word/(document|header\d*|footer\d*)\.xml$")
_TAG_RE = re.compile(r"<[^>]+>")

# Non-text markup that, sitting inside a {{...}}, blocks the run merge and
# leaves the field genuinely fragmented (mapped to a plain-French cause).
_STRUCTURAL = {
    "<w:br": "un saut de ligne (Maj+Entrée) à l'intérieur du champ",
    "<w:drawing": "une image ou une zone de texte à l'intérieur du champ",
    "<w:fldSimple": "un champ Word (date/renvoi) à l'intérieur du champ",
    "<w:fldChar": "un champ Word (date/renvoi) à l'intérieur du champ",
    "<w:bookmarkStart": "un signet à l'intérieur du champ",
    "<w:commentReference": "un commentaire à l'intérieur du champ",
    "<w:tab": "une tabulation à l'intérieur du champ",
    "<w:hyperlink": "un lien hypertexte à l'intérieur du champ",
}


def _targets_joined(data: bytes) -> str:
    """Every fill-target XML, run-normalized then joined (for cause lookup)."""
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if _TARGET_RE.match(name):
                parts.append(
                    _normalize_runs(zf.read(name).decode("utf-8", errors="replace"))
                )
    return "\n".join(parts)


def _structural_causes(xml: str, name: str) -> list[str]:
    """Structural markers found inside a ``{{name}}`` span in the raw XML."""
    causes: set[str] = set()
    for match in re.finditer(r"\{\{.*?\}\}", xml, re.DOTALL):
        span = match.group(0)
        if _TAG_RE.sub("", span)[2:-2].strip() != name:
            continue
        for marker, label in _STRUCTURAL.items():
            if marker in span:
                causes.add(label)
    return sorted(causes)


def main(path: str) -> int:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        print(f"Impossible d'ouvrir le fichier : {exc}")
        return 1

    validation = validate_template(data)
    if validation.errors:
        print("Le fichier n'a pas pu être lu comme gabarit :")
        for err in validation.errors:
            print("  -", err)
        return 1

    placeholders = validation.placeholders
    suspects = set(validation.split_run_suspects)
    classification = classify_placeholders(placeholders)

    print(f"{len(placeholders)} champ(s) détecté(s) :\n")
    for name in placeholders:
        if name in classification.auto:
            kind = "AUTO"     # rempli automatiquement
        elif name in classification.manual:
            kind = "SAISIE"   # demandé dans la fenêtre
        else:
            kind = "WORD"     # laissé tel quel, à compléter dans Word
        flag = "   <-- FRAGMENTÉ" if name in suspects else ""
        print(f"  [{kind:6}] {{{{{name}}}}}{flag}")

    if not suspects:
        print("\nOK — aucun champ fragmenté ; tous les champs reconnus se rempliront.")
        return 0

    joined = _targets_joined(data)
    print(
        "\nChamps encore fragmentés (scindés par Word d'une façon non "
        "réparable automatiquement) :"
    )
    for name in sorted(suspects):
        causes = _structural_causes(joined, name)
        detail = " ; ".join(causes) if causes else "runs non fusionnables"
        print(f"  - {{{{{name}}}}} — cause probable : {detail}")
    print(
        "\nDans Word : trouvez ce champ, retirez l'élément en cause entre les "
        "accolades (ou retapez le champ d'un seul trait), puis téléversez à "
        "nouveau le gabarit."
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage : python -m scripts.diagnose_gabarit <gabarit.docx>")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
