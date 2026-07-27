"""Rapprochement de noms (L3 §5.2) — module pur.

Ce que ces tests protègent, c'est surtout la MODESTIE de l'outil : il propose,
il ne tranche pas. Les cas limites ci-dessous existent pour que personne ne
soit tenté d'en faire un détecteur de conflits.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import rapprochement as rp  # noqa: E402


def _noms(*paires):
    return list(paires)


def test_nom_identique_malgre_accents_et_casse():
    trouves = rp.candidats("BETON NORD", _noms(("p1", "Béton Nord")))
    assert [c.cle for c in trouves] == ["p1"]
    assert trouves[0].motif == "nom identique"


def test_forme_juridique_ignoree():
    """« Béton Nord » ↔ « Béton Nord inc. » : c'est le cas le plus fréquent,
    selon que le client écrit la forme juridique ou non."""
    trouves = rp.candidats("Béton Nord", _noms(("p1", "Béton Nord inc.")))
    assert [c.cle for c in trouves] == ["p1"]


def test_prenom_nom_inverses_restent_rapproches():
    trouves = rp.candidats("Tremblay, Jean", _noms(("p1", "Jean Tremblay")))
    assert [c.cle for c in trouves] == ["p1"]


def test_un_seul_jeton_commun_banal_ne_suffit_pas():
    """« Jean Tremblay » et « Jean Bouchard » ne sont pas la même personne."""
    assert rp.candidats("Jean Tremblay", _noms(("p1", "Jean Bouchard"))) == []


def test_un_seul_jeton_suffit_s_il_constitue_tout_un_nom():
    """« Tremblay » (raison sociale ou nom seul) contre « Jean Tremblay » :
    à montrer, car c'est peut-être la même personne."""
    trouves = rp.candidats("Tremblay", _noms(("p1", "Jean Tremblay")))
    assert [c.cle for c in trouves] == ["p1"]


def test_les_plus_surs_viennent_en_premier():
    trouves = rp.candidats("Béton Nord", _noms(
        # Mêmes jetons, ordre inversé : ni identique, ni inclus.
        ("p3", "Nord Béton Québec"),
        ("p2", "Béton Nord inc."),
        ("p1", "béton nord"),
    ))
    assert [c.motif for c in trouves] == [
        "nom identique", "nom très proche", "jetons communs",
    ]
    assert [c.cle for c in trouves] == ["p1", "p2", "p3"]


def test_un_nom_plus_long_qui_contient_la_cible_est_tres_proche():
    """« Béton Nord Construction Québec » contient « Béton Nord » : c'est un
    rapprochement fort, pas un simple partage de jetons."""
    trouves = rp.candidats(
        "Béton Nord", _noms(("p1", "Béton Nord Construction Québec"))
    )
    assert [c.motif for c in trouves] == ["nom très proche"]


def test_un_nom_vide_ne_rapproche_rien():
    assert rp.candidats("", _noms(("p1", "Béton Nord"))) == []
    assert rp.candidats("   ", _noms(("p1", "Béton Nord"))) == []
    assert rp.candidats("...", _noms(("p1", "Béton Nord"))) == []


def test_un_existant_vide_est_ignore_sans_planter():
    assert rp.candidats("Béton Nord", _noms(("p1", ""), ("p2", None))) == []


def test_les_civilites_ne_creent_pas_de_faux_rapprochement():
    """Sans mots vides, « Me Jean Tremblay » et « Me Paul Gagnon »
    partageraient « me » — un jeton qui ne discrimine rien."""
    assert rp.candidats(
        "Me Jean Tremblay", _noms(("p1", "Me Paul Gagnon"))
    ) == []


def test_aucun_verdict_seulement_des_candidats():
    """Le contrat du module : une liste, jamais un booléen « conflit »."""
    resultat = rp.candidats("Béton Nord", _noms(("p1", "Béton Nord inc.")))
    assert isinstance(resultat, list)
    assert all(isinstance(c, rp.Candidat) for c in resultat)
    assert not hasattr(rp, "est_en_conflit")
