"""Route-side handling of manual gabarit fields (Sept. 2026).

Covers the two seams the popup owns: collecting a submitted value (defaults,
the ALL-CAPS rule, and the option guard the ``<select>`` alone used to
provide) and deciding whether a resolved value needs a ``<textarea>``.

Pure enough to run without Firestore: the Flask client is mocked away and the
helpers are exercised inside a bare request context.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

import pytest
from flask import Flask

with mock.patch("google.cloud.firestore.Client"):
    from routes.doc_templates import (
        _MULTILINE_MAX_CHARS,
        _SCALAR_MAX_CHARS,
        _ManualOptionError,
        _collect_values,
    )
from utils.template_fields import EMPTY_OPTION_VALUE

app = Flask(__name__)

BLANK_LINE = "\n\n"


def _collect(placeholders, form):
    with app.test_request_context("/gabarits/generer", method="POST", data=form):
        return _collect_values({"placeholders": placeholders})


# ── The option guard ───────────────────────────────────────────────────
#
# Until Sept. 2026 the <select> was the ONLY constraint: _collect_values
# truncated the raw string and wrote it into the letter. A stale tab or a
# crafted POST could therefore print any mention at all.

def test_a_value_outside_the_option_list_is_refused_by_name():
    with pytest.raises(_ManualOptionError) as exc:
        _collect(["privilège"], {"champ__privilège": "TOTALEMENT INVENTÉ"})
    assert exc.value.field_name == "privilège"


def test_each_offered_option_is_accepted():
    for value in ("SOUS TOUTES RÉSERVES", "SANS PRÉJUDICE", "—"):
        values, _ = _collect(["privilège"], {"champ__privilège": value})
        assert values["privilège"] == value


def test_free_text_manual_fields_are_not_constrained():
    values, _ = _collect(["objet_lettre"], {"champ__objet_lettre": "Mise en demeure"})
    assert values["objet_lettre"] == "Mise en demeure"


def test_auto_fields_are_never_option_checked():
    # Only manual fields carry options; an auto value must pass through even
    # though it shares the same form prefix.
    values, _ = _collect(["dossier.titre"], {"champ__dossier.titre": "N'importe quoi"})
    assert values["dossier.titre"] == "N'importe quoi"


# ── Defaults, the two silences, and the ALL-CAPS rule ──────────────────

def test_untouched_field_falls_to_its_default_then_to_the_loud_marker():
    values, missing = _collect(["pièces_jointes"], {"champ__pièces_jointes": ""})
    assert values["pièces_jointes"] == "Aucune"
    assert missing == 0
    values, missing = _collect(["privilège"], {"champ__privilège": ""})
    assert values["privilège"] == "[À COMPLÉTER : privilège]"
    assert missing == 1


def test_choosing_aucune_mention_prints_nothing_and_is_not_counted_missing():
    values, missing = _collect(
        ["privilège"], {"champ__privilège": EMPTY_OPTION_VALUE}
    )
    assert values["privilège"] == ""
    assert missing == 0


def test_capitalised_manual_placeholder_is_collected_and_upper_cased():
    # {{PRIVILÈGE}} is what both production correspondence gabarits write. It
    # used to be passthrough — never prompted, never collected.
    values, _ = _collect(
        ["PRIVILÈGE", "TRANSMISSION_LETTRE"],
        {"champ__PRIVILÈGE": "SANS PRÉJUDICE",
         "champ__TRANSMISSION_LETTRE": "courriel"},
    )
    assert values["PRIVILÈGE"] == "SANS PRÉJUDICE"
    assert values["TRANSMISSION_LETTRE"] == "COURRIEL"


def test_capitalised_manual_placeholder_does_not_raise_keyerror():
    # The regression that mattered: a bare MANUAL_FIELDS[name] index.
    values, _ = _collect(["PIÈCES_JOINTES"], {"champ__PIÈCES_JOINTES": ""})
    assert values["PIÈCES_JOINTES"] == "AUCUNE"


# ── The multiline cap ──────────────────────────────────────────────────

def test_a_multi_paragraph_value_is_not_truncated_at_the_scalar_cap():
    # A party block is a different animal from letter metadata: five parties
    # with their addresses already exceed 2000 characters.
    block = BLANK_LINE.join(f"Partie {i}, {'x' * 400}" for i in range(8))
    assert len(block) > _SCALAR_MAX_CHARS
    values, _ = _collect(
        ["dossier.defendeurs_avec_adresse"],
        {"champ__dossier.defendeurs_avec_adresse": block},
    )
    assert values["dossier.defendeurs_avec_adresse"] == block
    assert len(block) < _MULTILINE_MAX_CHARS


def test_a_single_line_value_still_takes_the_scalar_cap():
    values, _ = _collect(
        ["objet_lettre"], {"champ__objet_lettre": "x" * (_SCALAR_MAX_CHARS + 500)}
    )
    assert len(values["objet_lettre"]) == _SCALAR_MAX_CHARS


def test_blank_lines_survive_collection_so_the_expansion_can_fire():
    # If they did not, docx_fill would treat the block as a scalar and every
    # party would land in one paragraph — silently.
    block = f"Alpha inc.{BLANK_LINE}Beta ltée"
    values, _ = _collect(
        ["dossier.defendeurs_avec_adresse"],
        {"champ__dossier.defendeurs_avec_adresse": block},
    )
    assert BLANK_LINE in values["dossier.defendeurs_avec_adresse"]


def test_the_multiline_repair_also_covers_dossier_sommaire():
    # {{dossier.sommaire}} has been documented as multi-paragraph since Phase H
    # ("blank-line-separated chunks expand into cloned paragraphs"), but it made
    # the same round trip through <input type=text> as everything else — so its
    # newlines were stripped and it always arrived as one paragraph. The party
    # block is what surfaced the trap; the repair is shared.
    sommaire = f"Premier paragraphe.{BLANK_LINE}Second paragraphe."
    values, _ = _collect(
        ["dossier.sommaire"], {"champ__dossier.sommaire": sommaire}
    )
    assert values["dossier.sommaire"] == sommaire
    assert len(values["dossier.sommaire"].split(BLANK_LINE)) == 2
