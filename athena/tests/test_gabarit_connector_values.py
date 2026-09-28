"""Les valeurs d'un remplissage par le connecteur (lot 2A, étape T8) —
``services.gabarit_champs.values_for_connector``, la moitié PURE de
``fill_gabarit``.

Le web remplit ce que le juriste a SOUMIS dans la fenêtre ; le connecteur
remplit ce que le SERVEUR résout, plus les deux seules choses que Claude
peut écrire : les BLOCS du gabarit (les noms que le catalogue ne connaît
pas — « {{FAITS}} ») et ses champs MANUELS. On épingle :

* qu'un champ que l'application résout n'est jamais « écrasé » par un bloc
  du même nom — il est refusé, en nommant pourquoi ;
* que les noms se comparent EXACTEMENT, comme list_templates les rapporte ;
* qu'un texte portant « {{ » ou « }} » est refusé — le moteur relirait
  « {{dossier.demandeur}} » écrit dans un bloc comme un champ à remplir et
  y imprimerait des données du dossier (constat de la revue du lot 2) ;
* qu'aucun message ne cite ce qui a été envoyé — seulement des positions et
  des noms que le GABARIT porte.
"""

import os
import pathlib
import sys
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from services import gabarit_champs as gc
    from services import gabarits as sg

from utils.template_fields import EMPTY_OPTION_VALUE, fallback_value  # noqa: E402

TEMPLATE = {"placeholders": [
    "dossier.titre", "TRIBUNAL", "objet_lettre", "privilège",
    "FAITS", "CONCLUSIONS", "civilité",
]}
RESOLVED = {"dossier.titre": "Tremblay c. Alpha", "TRIBUNAL": ""}


def _values(blocs=(), manuels=(), template=TEMPLATE, resolved=RESOLVED):
    return gc.values_for_connector(template, resolved, blocs=list(blocs),
                                   manuels=list(manuels))


def _refused(**kw) -> gc.GenerationRefused:
    with pytest.raises(gc.GenerationRefused) as exc:
        _values(**kw)
    return exc.value


def test_the_server_resolves_and_claude_writes_only_blocs_and_manual_fields():
    out = _values(
        blocs=[{"nom": "FAITS", "contenu": "Un.\n\nDeux."},
               {"nom": "CONCLUSIONS", "contenu": "## Titre\n\n**gras**",
                "markdown": True}],
        manuels=[{"nom": "objet_lettre", "valeur": "Mise en demeure"}],
    )
    assert out.values["dossier.titre"] == "Tremblay c. Alpha"
    assert out.values["TRIBUNAL"] == fallback_value("TRIBUNAL", is_auto=True)
    assert out.values["objet_lettre"] == "Mise en demeure"
    assert out.values["FAITS"] == "Un.\n\nDeux."
    assert out.rich_values == {"CONCLUSIONS": "## Titre\n\n**gras**"}
    assert "CONCLUSIONS" not in out.values          # never in both (engine raises)
    assert "civilité" not in out.values             # open: left for Word
    assert out.auto_resolved == ("dossier.titre",)
    assert out.auto_missing == ("TRIBUNAL",)
    assert out.manual_missing == ("privilège",)     # no default: [À COMPLÉTER]
    assert out.blocs_plain == ("FAITS",) and out.blocs_markdown == ("CONCLUSIONS",)
    assert out.blocs_open == ("civilité",)


def test_an_auto_field_named_as_a_bloc_is_refused_never_overridden():
    exc = _refused(blocs=[{"nom": "dossier.titre", "contenu": "Autre titre"}])
    assert exc.reason == "bloc_unknown"
    assert "l'application remplit elle-même" in exc.message
    assert "Autre titre" not in exc.message


def test_a_manual_field_named_as_a_bloc_points_to_champs_manuels():
    exc = _refused(blocs=[{"nom": "objet_lettre", "contenu": "x"}])
    assert exc.reason == "bloc_unknown" and "`champs_manuels`" in exc.message


def test_names_compare_exactly_and_the_refusal_lists_the_blocs():
    exc = _refused(blocs=[{"nom": "faits", "contenu": "x"}])
    assert exc.reason == "bloc_unknown"
    assert "« FAITS »" in exc.message and "« CONCLUSIONS »" in exc.message
    assert "la casse compte" in exc.message
    # The caller's own string is never echoed back.
    assert "« faits »" not in exc.message


def test_a_name_twice_or_in_both_lists_is_refused():
    twice = _refused(blocs=[{"nom": "FAITS", "contenu": "a"},
                            {"nom": "FAITS", "contenu": "b"}])
    assert twice.reason == "bloc_conflict" and "n° 2" in twice.message
    manual_twice = _refused(manuels=[{"nom": "objet_lettre", "valeur": "a"},
                                     {"nom": "objet_lettre", "valeur": "b"}])
    assert manual_twice.reason == "bloc_conflict"


def test_a_manual_entry_that_is_not_a_manual_field_is_refused():
    exc = _refused(manuels=[{"nom": "FAITS", "valeur": "x"}])
    assert exc.reason == "manual_unknown" and "`blocs`" in exc.message
    exc = _refused(manuels=[{"nom": "inconnu", "valeur": "x"}])
    assert exc.reason == "manual_unknown" and "« objet_lettre »" in exc.message


def test_a_manual_value_off_its_list_is_refused_the_sentinel_is_not():
    exc = _refused(manuels=[{"nom": "privilège", "valeur": "Secret d'État"}])
    assert exc.reason == "manual_option_invalid"
    assert "Secret d'État" not in exc.message
    out = _values(manuels=[{"nom": "privilège", "valeur": EMPTY_OPTION_VALUE}])
    assert out.values["privilège"] == ""            # « (aucune mention) »
    assert "privilège" not in out.manual_missing     # a choice, not a gap


@pytest.mark.parametrize("text", [
    "Le demandeur {{dossier.demandeur}} réclame",
    "fin }} de bloc",
])
def test_a_text_carrying_placeholder_sigils_is_refused(text):
    """Review of lot 2: the engine's later passes rescan what an earlier one
    inserted — « {{dossier.demandeur}} » written in a bloc would print the
    dossier's data there."""
    exc = _refused(blocs=[{"nom": "FAITS", "contenu": text}])
    assert exc.reason == "value_refused" and "{{" in exc.message
    assert text not in exc.message
    exc = _refused(manuels=[{"nom": "objet_lettre", "valeur": text}])
    assert exc.reason == "value_refused"


def test_blank_control_and_oversized_texts_are_refused_never_repaired():
    assert _refused(blocs=[{"nom": "FAITS", "contenu": "  \n "}]).reason == (
        "value_refused")
    assert _refused(blocs=[{"nom": "FAITS", "contenu": "a\x07b"}]).reason == (
        "value_refused")
    long = "x" * (gc.BLOC_MAX_CHARS + 1)
    assert _refused(blocs=[{"nom": "FAITS", "contenu": long}]).reason == (
        "value_too_long")
    assert _refused(manuels=[{"nom": "objet_lettre",
                              "valeur": "x" * (gc.CHAMP_MANUEL_MAX_CHARS + 1)}]
                    ).reason == "value_too_long"
    # Tab, newline and carriage return are text.
    assert _values(blocs=[{"nom": "FAITS", "contenu": "a\tb\r\nc"}])


def test_the_total_of_the_blocs_is_bounded():
    template = {"placeholders": [f"B{i}" for i in range(4)]}
    chunk = "x" * gc.BLOC_MAX_CHARS
    exc = _refused(template=template, resolved={},
                   blocs=[{"nom": f"B{i}", "contenu": chunk} for i in range(4)])
    assert exc.reason == "value_too_long" and "au total" in exc.message


def test_the_counts_are_bounded_in_the_service_too():
    template = {"placeholders": [f"B{i}" for i in range(13)]}
    exc = _refused(template=template, resolved={},
                   blocs=[{"nom": f"B{i}", "contenu": "x"} for i in range(13)])
    assert exc.reason == "too_many_items"


def test_the_ceilings_are_reexported_by_the_write_half():
    assert sg.values_for_connector is gc.values_for_connector
    assert sg.BLOC_MAX_CHARS == 20_000 and sg.BLOCS_TOTAL_MAX_CHARS == 60_000
    assert sg.BLOCS_MAX_ITEMS == sg.CHAMPS_MANUELS_MAX_ITEMS == 12
    assert sg.CHAMP_MANUEL_MAX_CHARS == gc.MANUAL_MAX_CHARS
